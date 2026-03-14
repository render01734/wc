#!/usr/bin/env python3
"""
⛏️  Minecraft Shared World Engine  ─  Tek Dosya
═══════════════════════════════════════════════════════════
  • Tüm .ini config'ler bu dosyada gömülü
  • Minecraft 1.8 protokol MITM (offline = şifresiz = okunabilir)
  • Shared World State: blok değişiklikleri tüm sunucularda senkron
  • Tüm oyuncular aynı Cuberite'ye yönlenir → GERÇEK ortak dünya
  • Cross-server chat: farklı instance'lardaki oyuncular mesajlaşır
  • HTTP durum sayfası (Render.com sağlık kontrolü)

ENGINE_MODE=proxy      → wc-tsgd.onrender.com üzerinde çalışır
ENGINE_MODE=gameserver → Cuberite instance'ı (diğer servisler)
ENGINE_MODE=all        → Proxy + Cuberite aynı container (test)
"""

import asyncio, json, os, pathlib, struct, sys
import threading, zlib, time, http.server, urllib.request
import subprocess, signal, glob

MODE       = os.environ.get("ENGINE_MODE", "gameserver")
HTTP_PORT  = int(os.environ.get("PORT", 8080))
MC_PORT    = int(os.environ.get("MC_PORT", 25565))
DATA_DIR   = os.environ.get("DATA_DIR", "/data")
SERVER_DIR = os.environ.get("SERVER_DIR", "/server")
BORE_FILE  = "/tmp/bore_address.txt"
STATE_FILE = f"{DATA_DIR}/world_state.json"
BACKENDS_FILE = f"{DATA_DIR}/backends.json"

# ════════════════════════════════════════════════════════════
#  GÖMÜLÜ CONFIG DOSYALARI
# ════════════════════════════════════════════════════════════

SETTINGS_INI = """
[Authentication]
Authenticate=0
OnlineMode=0
ServerID=CuberiteEngine
PlayerRestrictIP=0

[Server]
Description=Shared World Engine
MaxPlayers=20
Ports=25565
ResourcePackUrl=
ResourcePackHash=

[Worlds]
DefaultWorld=world
World=world

[Deadlock]
DeadlockDetect=1
IntervalSec=20

[Limits]
MaxPlayerInWorld=20

[Ranking]
DbFile=Ranking.sqlite

[RCON]
Enabled=0

[SlowSQL]
LogSlowQueries=0
""".strip()

WEBADMIN_INI = """
[WebAdmin]
Enabled=0
Port=8081
""".strip()

WORLD_INI = """
[General]
Gamemode=1
WorldType=FLAT
AllowCommands=1
AllowFlight=1

[Mechanics]
CommandBlocksEnabled=0
UseChatPrefixes=1

[SpawnPosition]
MaxViewDistance=4
X=0
Y=5
Z=0

[Lighting]
AnimatedTimeSpeed=20

[Mobs]
AnimalsOn=0
MaxAnimals=0
MaxMonsters=0
MonstersOn=0
WolvesOn=0

[Weather]
ChangeWeather=0

[Tick]
TicksPerSecond=20

[Generator]
BiomeGen=Constant
ShapeGen=HeightMap
HeightGen=Flat
FlatHeight=1
CompositionGen=SameBlock
SameBlockType=bedrock
SameBlockBedrocked=0
Finishers=
""".strip()

GROUPS_INI = """
[Default]
Permissions=core.spawn,core.help,core.list
Color=f
Inherits=

[Admin]
Permissions=*
Color=c
Inherits=Default
""".strip()


def write_configs(server_dir: str = SERVER_DIR) -> None:
    bins = glob.glob(f"{server_dir}/**/Cuberite", recursive=True)
    if bins:
        server_dir = str(pathlib.Path(bins[0]).parent)
    files = {
        f"{server_dir}/settings.ini":    SETTINGS_INI,
        f"{server_dir}/webadmin.ini":    WEBADMIN_INI,
        f"{server_dir}/world/world.ini": WORLD_INI,
        f"{server_dir}/groups.ini":      GROUPS_INI,
        "/server/world/world.ini":       WORLD_INI,
    }
    for path, content in files.items():
        try:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(path).write_text(content + "\n", encoding="utf-8")
            print(f"[CFG] {path}")
        except Exception as e:
            print(f"[CFG] HATA {path}: {e}")


# ════════════════════════════════════════════════════════════
#  MINECRAFt 1.8 PROTOKOLü  (offline = şifresiz)
# ════════════════════════════════════════════════════════════
#
#  Şifreli değil → paketleri okuyabilir ve değiştirebiliriz!
#
#  Paket formatı (sıkıştırmasız):
#    [VarInt uzunluk][VarInt paket_id][...veri...]
#
#  Paket formatı (sıkıştırmalı, Set Compression sonrası):
#    [VarInt toplam_uzunluk][VarInt sıkıştırılmamış_uzunluk][...veri...]
#    sıkıştırılmamış_uzunluk=0  → sıkıştırılmamış
#    sıkıştırılmamış_uzunluk>0  → zlib sıkıştırılmış

def vi_enc(v: int) -> bytes:
    """VarInt encode"""
    r = bytearray()
    while True:
        b = v & 0x7F; v >>= 7
        if v: b |= 0x80
        r.append(b)
        if not v: break
    return bytes(r)

def vi_dec(data: bytes, pos: int = 0):
    """VarInt decode → (value, new_pos)"""
    r = shift = 0
    while True:
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << shift
        if not (b & 0x80): return r, pos
        shift += 7
        if shift >= 35: raise ValueError("VarInt çok uzun")

async def vi_rd(reader: asyncio.StreamReader) -> int:
    """Async stream'den VarInt oku"""
    r = shift = 0
    while True:
        b = (await reader.readexactly(1))[0]
        r |= (b & 0x7F) << shift
        if not (b & 0x80): return r
        shift += 7
        if shift >= 35: raise ValueError("VarInt çok uzun")

async def pkt_read(reader: asyncio.StreamReader, comp: int = -1):
    """Bir MC paketi oku. → (packet_id, payload_bytes, raw_bytes)"""
    length = await vi_rd(reader)
    raw = await reader.readexactly(length)

    if comp < 0:
        pid, pos = vi_dec(raw)
        return pid, raw[pos:], vi_enc(length) + raw
    else:
        data_len, pos = vi_dec(raw)
        inner = raw[pos:]
        if data_len == 0:
            pid, pos2 = vi_dec(inner)
            return pid, inner[pos2:], vi_enc(length) + raw
        else:
            decompressed = zlib.decompress(inner)
            pid, pos2 = vi_dec(decompressed)
            return pid, decompressed[pos2:], vi_enc(length) + raw

def pkt_make(pid: int, payload: bytes, comp: int = -1) -> bytes:
    """MC paketi oluştur"""
    id_bytes = vi_enc(pid)
    data = id_bytes + payload
    if comp < 0:
        return vi_enc(len(data)) + data
    if len(data) < comp:
        inner = vi_enc(0) + data
        return vi_enc(len(inner)) + inner
    compressed = zlib.compress(data)
    inner = vi_enc(len(data)) + compressed
    return vi_enc(len(inner)) + inner

def mc_str_enc(s: str) -> bytes:
    """MC String encode (VarInt uzunluk + UTF-8)"""
    b = s.encode("utf-8")
    return vi_enc(len(b)) + b

def mc_str_dec(data: bytes, pos: int = 0):
    """MC String decode → (str, new_pos)"""
    length, pos = vi_dec(data, pos)
    return data[pos:pos + length].decode("utf-8", errors="replace"), pos + length

# MC 1.8 Position (64-bit long packed)
def pos_enc(x: int, y: int, z: int) -> bytes:
    v = ((x & 0x3FFFFFF) << 38) | ((y & 0xFFF) << 26) | (z & 0x3FFFFFF)
    return struct.pack(">q", v)

def pos_dec(data: bytes, pos: int = 0):
    v = struct.unpack_from(">q", data, pos)[0]
    x = v >> 38;       y = (v >> 26) & 0xFFF;  z = v & 0x3FFFFFF
    if x >= (1 << 25): x -= (1 << 26)
    if z >= (1 << 25): z -= (1 << 26)
    return x, y, z, pos + 8

# Paket ID'leri — MC 1.8 (protocol 47)
#   Play S→C (sunucu → oyuncu):
PID_CHAT_SC     = 0x02   # Sohbet mesajı
PID_POS_LOOK    = 0x08   # Player Position And Look (oyuncu yüklendi)
PID_MULTI_BLK   = 0x22   # Multi Block Change
PID_BLOCK_CHG   = 0x23   # Block Change
#   Play C→S (oyuncu → sunucu):
PID_CHAT_CS     = 0x01   # Sohbet mesajı (client → server)
#   Login S→C:
PID_LOGIN_OK    = 0x02   # Login Success  → play state
PID_SET_COMP    = 0x03   # Set Compression


# ════════════════════════════════════════════════════════════
#  PAYLAŞIMLI DÜNYA STATE  (Blok değişiklikleri)
# ════════════════════════════════════════════════════════════

class WorldState:
    """
    Tüm oyuncuların yaptığı blok değişikliklerini saklar.
    Yeni oyuncu bağlandığında tüm geçmişi replay eder → aynı dünyayı görür.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.blocks: dict[str, int] = {}   # "x,y,z" → block_id (varint birleşik)
        self._load()

    def record(self, x: int, y: int, z: int, block_id: int):
        key = f"{x},{y},{z}"
        with self._lock:
            if block_id == 0:           # hava → kaydı sil
                self.blocks.pop(key, None)
            else:
                self.blocks[key] = block_id
        self._save()

    def record_multi(self, chunk_x: int, chunk_z: int, records):
        """records: [(rel_x, y, rel_z, block_id), ...]"""
        with self._lock:
            for rx, ry, rz, bid in records:
                x = chunk_x * 16 + rx
                z = chunk_z * 16 + rz
                key = f"{x},{ry},{z}"
                if bid == 0:
                    self.blocks.pop(key, None)
                else:
                    self.blocks[key] = bid
        self._save()

    def make_replay_packets(self, comp: int = -1) -> list[bytes]:
        """Tüm blok değişikliklerini oyuncuya gönderilecek paket listesi olarak döndür"""
        pkts = []
        with self._lock:
            items = list(self.blocks.items())
        for key, bid in items:
            try:
                x, y, z = map(int, key.split(","))
                payload = pos_enc(x, y, z) + vi_enc(bid)
                pkts.append(pkt_make(PID_BLOCK_CHG, payload, comp))
            except Exception:
                pass
        return pkts

    def _save(self):
        try:
            pathlib.Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(STATE_FILE).write_text(
                json.dumps({"blocks": self.blocks}), encoding="utf-8"
            )
        except Exception:
            pass

    def _load(self):
        try:
            d = json.loads(pathlib.Path(STATE_FILE).read_text())
            self.blocks = d.get("blocks", {})
            print(f"[STATE] {len(self.blocks)} blok değişikliği yüklendi.")
        except Exception:
            self.blocks = {}

world_state = WorldState()


# ════════════════════════════════════════════════════════════
#  AKTİF BAĞLANTILAR  (cross-server chat için)
# ════════════════════════════════════════════════════════════

_active_lock = asyncio.Lock()
_active: list["PlayerConn"] = []

async def _broadcast_chat(sender_conn: "PlayerConn", msg: str):
    """Bir oyuncunun sohbet mesajını DİĞER tüm bağlı oyunculara gönder"""
    async with _active_lock:
        targets = [c for c in _active if c is not sender_conn and c.play_state]
    payload = mc_str_enc(json.dumps({"text": msg, "color": "white"})) + b'\x00'
    for conn in targets:
        try:
            pkt = pkt_make(PID_CHAT_SC, payload, conn.s2c_comp)
            conn.client_w.write(pkt)
            await conn.client_w.drain()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════
#  BACKEND YÖNETİMİ
# ════════════════════════════════════════════════════════════

def load_backends() -> list[dict]:
    try:
        return json.loads(pathlib.Path(BACKENDS_FILE).read_text())
    except Exception:
        return []

def save_backends(backends: list[dict]):
    pathlib.Path(BACKENDS_FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(BACKENDS_FILE).write_text(json.dumps(backends, indent=2))

def pick_backend() -> dict | None:
    """
    STRATEJİ: En çok oyuncuya sahip sunucuya yönlendir.
    Böylece tüm oyuncular aynı Cuberite'de → gerçek ortak dünya.
    Sadece birincil doluysa (max) ikinciye geç.
    """
    backends = load_backends()
    if not backends:
        return None
    # Aktif bağlantı sayısına göre sırala (çoktan aza)
    counts = {}
    for c in _active:
        k = f"{c.backend_host}:{c.backend_port}"
        counts[k] = counts.get(k, 0) + 1
    # En çok oyuncusu olan ama MAX_PLAYERS'ı aşmamış sunucu
    MAX = 19
    for b in sorted(backends, key=lambda x: counts.get(f"{x['host']}:{x['port']}", 0), reverse=True):
        k = f"{b['host']}:{b['port']}"
        if counts.get(k, 0) < MAX:
            return b
    return backends[0]  # hepsini aştıysa ilkine gönder


# ════════════════════════════════════════════════════════════
#  OYUNCU BAĞLANTISI  (MITM proxy + paket müdahalesi)
# ════════════════════════════════════════════════════════════

class PlayerConn:
    def __init__(self, cr, cw):
        self.client_r: asyncio.StreamReader = cr
        self.client_w: asyncio.StreamWriter = cw
        self.server_r: asyncio.StreamReader | None = None
        self.server_w: asyncio.StreamWriter | None = None
        self.state     = "handshake"   # handshake | login | play
        self.play_state = False
        self.c2s_comp  = -1   # client→server compression
        self.s2c_comp  = -1   # server→client compression
        self.username  = "?"
        self.backend_host = ""
        self.backend_port = 0
        self.peer = cw.get_extra_info("peername", ("?", 0))[0]
        self._replay_sent = False

    async def connect_backend(self, backend: dict):
        self.backend_host = backend["host"]
        self.backend_port = backend["port"]
        self.server_r, self.server_w = await asyncio.open_connection(
            self.backend_host, self.backend_port, limit=2**20
        )

    # ── Sunucu → Oyuncu akışı (okunur + müdahale edilir) ────
    async def pipe_s2c(self):
        try:
            while True:
                pid, payload, raw = await pkt_read(self.server_r, self.s2c_comp)

                # Login Success → play state'e geç
                if self.state == "login" and pid == PID_LOGIN_OK:
                    self.state = "play"
                    self.client_w.write(raw); await self.client_w.drain()
                    continue

                # Set Compression (login state)
                if self.state == "login" and pid == PID_SET_COMP:
                    threshold, _ = vi_dec(payload)
                    self.s2c_comp = threshold
                    self.client_w.write(raw); await self.client_w.drain()
                    continue

                if self.state != "play":
                    self.client_w.write(raw); await self.client_w.drain()
                    continue

                # ─ Play state: paketleri işle ─────────────

                # Block Change → state'e kaydet
                if pid == PID_BLOCK_CHG and len(payload) >= 9:
                    try:
                        x, y, z, pos2 = pos_dec(payload)
                        bid, _ = vi_dec(payload, pos2)
                        world_state.record(x, y, z, bid)
                    except Exception:
                        pass

                # Multi Block Change → her bloğu kaydet
                elif pid == PID_MULTI_BLK and len(payload) >= 8:
                    try:
                        cx = struct.unpack_from(">i", payload, 0)[0]
                        cz = struct.unpack_from(">i", payload, 4)[0]
                        count, pos2 = vi_dec(payload, 8)
                        records = []
                        for _ in range(count):
                            hp = payload[pos2]; pos2 += 1   # yüksek nibble=x, düşük=z
                            ry = payload[pos2]; pos2 += 1
                            bid, pos2 = vi_dec(payload, pos2)
                            records.append(((hp >> 4) & 0xF, ry, hp & 0xF, bid))
                        world_state.record_multi(cx, cz, records)
                    except Exception:
                        pass

                # Player Position And Look → oyuncu yüklendi, replay gönder
                elif pid == PID_POS_LOOK and not self._replay_sent:
                    self._replay_sent = True
                    self.client_w.write(raw); await self.client_w.drain()
                    await self._send_block_replay()
                    continue

                self.client_w.write(raw); await self.client_w.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"[S→C] {self.username} hata: {e}")
        finally:
            await self._cleanup()

    # ── Oyuncu → Sunucu akışı (okunur + cross-server chat) ─
    async def pipe_c2s(self):
        try:
            while True:
                pid, payload, raw = await pkt_read(self.client_r, self.c2s_comp)

                # Login Start → kullanıcı adını al
                if self.state == "login" and pid == 0x00:
                    try:
                        self.username, _ = mc_str_dec(payload)
                        print(f"[JOIN] {self.username} @ {self.backend_host}:{self.backend_port}")
                    except Exception:
                        pass

                # Handshake → next state kontrolü
                elif self.state == "handshake" and pid == 0x00:
                    try:
                        # [VarInt proto][String host][UShort port][VarInt nextState]
                        pos2 = 0
                        _, pos2 = vi_dec(payload, pos2)        # proto version
                        _, pos2 = mc_str_dec(payload, pos2)    # server host
                        pos2 += 2                              # port (ushort)
                        next_s, _ = vi_dec(payload, pos2)
                        if next_s == 2:
                            self.state = "login"
                    except Exception:
                        pass

                # Chat mesajı → cross-server broadcast
                elif self.state == "play" and pid == PID_CHAT_CS:
                    try:
                        msg, _ = mc_str_dec(payload)
                        # Diğer sunuculardaki oyunculara ilet
                        full = f"[{self.username}] {msg}"
                        asyncio.ensure_future(_broadcast_chat(self, full))
                    except Exception:
                        pass

                self.server_w.write(raw); await self.server_w.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"[C→S] {self.username} hata: {e}")
        finally:
            await self._cleanup()

    async def _send_block_replay(self):
        """Tüm kayıtlı blok değişikliklerini oyuncuya gönder"""
        pkts = world_state.make_replay_packets(self.s2c_comp)
        if pkts:
            print(f"[REPLAY] {self.username} için {len(pkts)} blok gönderiliyor...")
            for pkt in pkts:
                self.client_w.write(pkt)
            await self.client_w.drain()
            print(f"[REPLAY] ✓ Tamamlandı")

    async def _cleanup(self):
        async with _active_lock:
            if self in _active:
                _active.remove(self)
                print(f"[QUIT] {self.username} ayrıldı ({len(_active)} aktif)")
        for w in (self.client_w, self.server_w):
            if w:
                try: w.close()
                except Exception: pass

    async def run(self):
        backend = pick_backend()
        if not backend:
            print(f"[WARN] Backend yok! {self.peer} bağlanamadı.")
            self.client_w.close(); return
        try:
            await self.connect_backend(backend)
        except Exception as e:
            print(f"[ERR] Backend bağlantı hatası: {e}")
            self.client_w.close(); return

        async with _active_lock:
            _active.append(self)
        print(f"[CONN] {self.peer} → {backend['host']}:{backend['port']} ({len(_active)} aktif)")

        await asyncio.gather(self.pipe_s2c(), self.pipe_c2s())


async def handle_player(cr, cw):
    conn = PlayerConn(cr, cw)
    await conn.run()


# ════════════════════════════════════════════════════════════
#  HTTP DURUM SAYFASI
# ════════════════════════════════════════════════════════════

HTML = """\
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>⛏️ Minecraft Engine</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#1a1a2e;color:#e0e0e0;font-family:'Courier New',monospace;
          display:flex;align-items:center;justify-content:center;
          min-height:100vh;padding:20px}}
    .card{{background:#16213e;border:2px solid #0f3460;border-radius:12px;
           padding:36px;max-width:720px;width:100%;text-align:center;
           box-shadow:0 0 30px rgba(15,52,96,.5)}}
    h1{{font-size:1.8rem;color:#4ecca3;margin-bottom:6px}}
    .sub{{color:#888;margin-bottom:24px;font-size:.85rem}}
    .addr{{background:#0f3460;border:1px solid #4ecca3;border-radius:8px;
           padding:14px;margin:16px 0;font-size:1.2rem;color:#4ecca3;word-break:break-all}}
    table{{width:100%;border-collapse:collapse;margin:14px 0;text-align:left}}
    th{{color:#4ecca3;border-bottom:1px solid #0f3460;padding:8px 10px;font-size:.8rem}}
    td{{padding:8px 10px;font-size:.88rem;border-bottom:1px solid #0f346033}}
    .on{{color:#4ecca3}}.off{{color:#e74c3c}}
    .dot{{display:inline-block;width:8px;height:8px;border-radius:50%;
          margin-right:5px;animation:pulse 1.5s infinite}}
    .dot.g{{background:#4ecca3}}.dot.r{{background:#e74c3c}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .badge{{display:inline-block;background:#0f3460;border:1px solid #4ecca3;
            color:#4ecca3;font-size:.72rem;padding:2px 8px;border-radius:20px;margin:3px}}
    .info{{font-size:.78rem;color:#555;margin-top:16px}}
    .stat{{font-size:.9rem;color:#aaa;margin:6px 0}}
  </style>
</head>
<body>
<div class="card">
  <h1>⛏️ Minecraft Shared World Engine</h1>
  <p class="sub">Cuberite &bull; Offline Mode &bull; Ortak Dünya &bull; bore.pub Tunnel</p>
  {addr_block}
  <div class="stat">🟢 Aktif Oyuncu: <b>{player_count}</b> &nbsp;|&nbsp; 🧱 Blok Değişikliği: <b>{block_count}</b></div>
  <table>
    <tr><th>Game Server</th><th>Oyuncu</th><th>Durum</th></tr>
    {rows}
  </table>
  <div style="margin-top:16px">
    <span class="badge">🪶 Ultra Hafif</span>
    <span class="badge">🔓 Crack Girişi</span>
    <span class="badge">🌍 Ortak Dünya</span>
    <span class="badge">💬 Cross-Server Chat</span>
    <span class="badge">🧱 Blok Senkron</span>
  </div>
  <div class="info">Her 5 saniyede otomatik yenilenir</div>
</div></body></html>"""


def _build_html():
    try:
        bore = pathlib.Path(BORE_FILE).read_text().strip()
        addr = f'<div class="addr">🌐 {bore}</div><p style="color:#aaa;font-size:.85rem">Minecraft → Sunucu Ekle → bu adresi gir</p>'
    except Exception:
        addr = '<p style="color:#f8b400">⏳ Tunnel başlatılıyor...</p>'

    backends = load_backends()
    counts: dict[str, int] = {}
    for c in list(_active):
        k = f"{c.backend_host}:{c.backend_port}"
        counts[k] = counts.get(k, 0) + 1

    rows = ""
    for b in backends:
        k = f"{b['host']}:{b['port']}"
        n = counts.get(k, 0)
        label = b.get("label", k)
        rows += f'<tr><td>{label}</td><td>{n} oyuncu</td><td><span class="dot g"></span><span class="on">Aktif</span></td></tr>'
    if not rows:
        rows = '<tr><td colspan="3" style="color:#888;text-align:center">Game server bekleniyor...</td></tr>'

    return HTML.format(
        addr_block=addr,
        player_count=len(_active),
        block_count=len(world_state.blocks),
        rows=rows,
    )


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                           # noqa: N802
        body = _build_html().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):                          # noqa: N802
        """Game server'ların proxy'ye kayıt olduğu endpoint"""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._r(400, "bad json"); return

        if self.path == "/api/register":
            host  = data.get("host"); port = data.get("port")
            label = data.get("label", f"{host}:{port}")
            if not host or not port:
                self._r(400, "missing host/port"); return
            backends = load_backends()
            key = f"{host}:{port}"
            found = False
            for b in backends:
                if f"{b['host']}:{b['port']}" == key:
                    b["label"] = label; found = True; break
            if not found:
                backends.append({"host": host, "port": int(port), "label": label})
            save_backends(backends)
            print(f"[REG] ✓ {label} ({key})")
            self._r(200, "ok")
        else:
            self._r(404, "not found")

    def _r(self, code, msg):
        b = msg.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def log_message(self, *_): pass


def run_http():
    srv = http.server.HTTPServer(("0.0.0.0", HTTP_PORT), _H)
    print(f"[HTTP] Port {HTTP_PORT}")
    srv.serve_forever()


# ════════════════════════════════════════════════════════════
#  BORE TUNNEL  (her modda çalışır)
# ════════════════════════════════════════════════════════════

def run_bore(port: int = MC_PORT):
    import re
    while True:
        try:
            pathlib.Path(BORE_FILE).unlink(missing_ok=True)
            proc = subprocess.Popen(
                ["bore", "local", str(port), "--to", "bore.pub"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                line = line.rstrip()
                print(f"[BORE] {line}")
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    addr = f"bore.pub:{m.group(1)}"
                    pathlib.Path(BORE_FILE).write_text(addr)
                    print(f"[BORE] ✓ ADRESİ: {addr}")
                    # Proxy'ye kayıt gönder (gameserver modundaysa)
                    if MODE == "gameserver":
                        _register_with_proxy(addr)
            proc.wait()
        except FileNotFoundError:
            print("[BORE] bore bulunamadı, 10sn bekleniyor...")
        except Exception as e:
            print(f"[BORE] hata: {e}")
        time.sleep(10)


def _register_with_proxy(bore_addr: str):
    """Game server kendini ana proxy'ye kaydeder"""
    proxy_url = os.environ.get("PROXY_URL", "")
    if not proxy_url:
        return
    label = os.environ.get("SERVER_LABEL", "GameServer")
    host, port_str = bore_addr.split(":")
    try:
        body = json.dumps({"host": host, "port": int(port_str), "label": label}).encode()
        req = urllib.request.Request(
            f"{proxy_url}/api/register", data=body,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[REG] ✓ Proxy'ye kayıt: {proxy_url}")
    except Exception as e:
        print(f"[REG] Kayıt hatası: {e}")


# ════════════════════════════════════════════════════════════
#  CUBERİTE BAŞLATICI
# ════════════════════════════════════════════════════════════

def run_cuberite():
    write_configs()

    # Eski world verisini sadece ilk seferde sil (bayrak dosyası)
    flag = "/server/world/.void_initialized"
    if not pathlib.Path(flag).exists():
        for wp in ["/server/world", "/server/Server/world"]:
            if pathlib.Path(wp).exists():
                for sub in ["regions", "nether", "end"]:
                    import shutil
                    shutil.rmtree(f"{wp}/{sub}", ignore_errors=True)
                for f in glob.glob(f"{wp}/*.mca") + glob.glob(f"{wp}/*.mcr"):
                    pathlib.Path(f).unlink(missing_ok=True)
        pathlib.Path("/server/world").mkdir(parents=True, exist_ok=True)
        pathlib.Path(flag).touch()
        print("[MC] İlk başlatma: eski dünya silindi.")

    mc_bin = next(iter(glob.glob("/server/**/Cuberite", recursive=True)), None)
    if not mc_bin:
        print("[MC] HATA: Cuberite bulunamadı!"); return

    mc_dir = str(pathlib.Path(mc_bin).parent)
    os.chmod(mc_bin, 0o755)
    print(f"[MC] Başlatılıyor: {mc_bin}")

    # stdin için kalıcı FIFO (TTY yok → EOF almasın)
    fifo = "/tmp/mc_stdin"
    pathlib.Path(fifo).unlink(missing_ok=True)
    os.mkfifo(fifo)
    tail = subprocess.Popen(["tail", "-f", "/dev/null"],
                            stdout=open(fifo, "wb"), stderr=subprocess.DEVNULL)
    while True:
        proc = subprocess.Popen(
            [mc_bin], cwd=mc_dir, stdin=open(fifo, "rb")
        )
        proc.wait()
        print("[MC] Cuberite kapandı, 5sn sonra yeniden başlıyor...")
        time.sleep(5)


# ════════════════════════════════════════════════════════════
#  ASYNC PROXY MOTORU
# ════════════════════════════════════════════════════════════

async def run_proxy_async():
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    if not pathlib.Path(BACKENDS_FILE).exists():
        save_backends([])

    server = await asyncio.start_server(handle_player, "0.0.0.0", MC_PORT, limit=2**20)
    print(f"[PROXY] ✓ Minecraft proxy port {MC_PORT}")
    print(f"[PROXY] Strateji: Tüm oyuncular → En Kalabalık sunucu (ortak dünya)")
    async with server:
        await server.serve_forever()


# ════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════

def main():
    print(f"""
╔══════════════════════════════════════════════════╗
║  ⛏️  Minecraft Shared World Engine               ║
║  Mod: {MODE:<42}║
╚══════════════════════════════════════════════════╝""")

    if MODE == "config":
        write_configs(); return

    # HTTP her modda çalışır
    threading.Thread(target=run_http, daemon=True).start()

    if MODE == "proxy":
        # bore + proxy async loop
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        asyncio.run(run_proxy_async())

    elif MODE == "gameserver":
        # bore (proxy'ye kayıt) + Cuberite
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        run_cuberite()   # blocking döngü

    elif MODE == "all":
        # Tek container: proxy + Cuberite (geliştirme / test)
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        threading.Thread(target=run_cuberite, daemon=True).start()
        # Cuberite'yi backend olarak kendine ekle
        time.sleep(3)
        save_backends([{"host": "127.0.0.1", "port": MC_PORT, "label": "LocalCuberite"}])
        asyncio.run(run_proxy_async())

    elif MODE == "http":
        # Sadece HTTP sunucusu (durum sayfası)
        run_http()

    else:
        print(f"Bilinmeyen mod: {MODE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
