#!/usr/bin/env python3
"""
⛏️  Minecraft Ultimate Bungee Network & Anti-Dupe Engine
═══════════════════════════════════════════════════════════
  • SQLite (aiosqlite) — Tak-Çalıştır, sıfır yapılandırma
  • Dinamik GitHub eklenti listesi — Otomatik senkronizasyon
  • Canlı Web Konsolu — Tüm ağı tek panelden yönet
  • PROXY + GAMESERVER + ALL mod desteği
  • Thread-safe, timeout korumalı, güvenli mimari
"""

import asyncio
import contextlib
import datetime
import glob
import http.server
import json
import os
import pathlib
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zlib
from collections import deque

# ══════════════════════════════════════════════════════════
#  YAPILANDIRMA
# ══════════════════════════════════════════════════════════

MODE          = os.environ.get("ENGINE_MODE", "gameserver")
if "wc-yccy" in os.environ.get("RENDER_EXTERNAL_HOSTNAME", ""):
    MODE = "all"

HTTP_PORT     = int(os.environ.get("PORT", 8080))
MC_PORT       = int(os.environ.get("MC_PORT", 25565))
CUBERITE_PORT = 25566 if MODE == "all" else MC_PORT

DATA_DIR      = os.environ.get("DATA_DIR", "/data")
SERVER_DIR    = os.environ.get("SERVER_DIR", "/server")
DB_FILE       = os.path.join(DATA_DIR, "hub.db")

# BUG FIX #1 — _active_players artık lock ile korunuyor.
# Önceden: HTTP handler thread'leri ile asyncio coroutine'leri aynı listeye
# eş zamanlı erişiyordu (race condition → crash / yanlış oyuncu sayısı).
_active_players  = []
_PLAYERS_LOCK    = threading.Lock()

_proxy_bore_addr = None
_cuberite_proc   = None
_STDIN_LOCK      = threading.Lock()

# BUG FIX #2 — _cmd_counter / _cmd_history artık kendi lock'u ile korunuyor.
# Önceden: birden fazla HTTP thread aynı anda _cmd_counter'ı arttırabiliyordu.
_cmd_counter  = 0
_cmd_history  = []
_CMD_LOCK     = threading.Lock()

# BUG FIX #3 — _last_script_update için lock eklendi.
# Önceden: POST handler'da yazılıyor, sync_loop'ta okunuyordu (unsynchronized).
_last_script_update = time.time()
_SCRIPT_TS_LOCK     = threading.Lock()

_pending_remote_logs = []
_LOG_LOCK            = threading.Lock()
SYSTEM_LOGS          = deque(maxlen=500)
_DB_LOCK             = threading.Lock()

# ── İzin verilen komut karakterleri (enjeksiyona karşı) ──────────────────────
_CMD_SAFE_PATTERN = re.compile(r'^[\w\s\.\-:/@#,!?\'"=+\[\]()öçşğüıÖÇŞĞÜİ]+$', re.UNICODE)


# ══════════════════════════════════════════════════════════
#  LOGLAMA
# ══════════════════════════════════════════════════════════

def log_msg(text: str) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line  = f"[{stamp}] {text}"
    SYSTEM_LOGS.append(line)
    print(line, flush=True)
    if MODE == "gameserver":
        with _LOG_LOCK:
            _pending_remote_logs.append(text)


# ══════════════════════════════════════════════════════════
#  VERİTABANI
# ══════════════════════════════════════════════════════════

async def init_db() -> None:
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_FILE) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS servers (
                    label           TEXT PRIMARY KEY,
                    host            TEXT,
                    port            INTEGER,
                    players         INTEGER DEFAULT 0,
                    last_seen       INTEGER,
                    restart_pending INTEGER DEFAULT 0,
                    server_id       TEXT
                );
                CREATE TABLE IF NOT EXISTS players (
                    username    TEXT PRIMARY KEY,
                    last_server TEXT
                );
            """)
            # Eski kayıtları temizle
            await db.execute(
                "DELETE FROM servers WHERE (? - last_seen) > 45",
                (int(time.time()),)
            )
            await db.commit()
        log_msg("[DB] SQLite veritabanı hazır.")
    except Exception as e:
        log_msg(f"[DB] HATA: Veritabanı oluşturulamadı → {e}")


def _sync_db_connect():
    """
    Sync thread'ler için güvenli SQLite bağlantısı.
    DATA_DIR her zaman var olmalı; yoksa oluştur.
    """
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ══════════════════════════════════════════════════════════
#  DİNAMİK GITHUB SCRIPT GÜNCELLEYİCİ
# ══════════════════════════════════════════════════════════

_FALLBACK_PLUGINS = ["wcmaster.lua"]

def update_and_configure(server_dir: str = SERVER_DIR) -> bool:
    base_url = "https://raw.githubusercontent.com/Exma0/va/refs/heads/main"
    list_url = f"{base_url}/list"

    log_msg("[GÜNCELLEME] GitHub eklenti listesi kontrol ediliyor...")
    try:
        req   = urllib.request.Request(list_url, headers={"Cache-Control": "no-cache"})
        lines = urllib.request.urlopen(req, timeout=15).read().decode("utf-8").splitlines()
    except Exception as e:
        log_msg(f"[GÜNCELLEME] Liste çekilemedi ({e}), yedek liste kullanılıyor.")
        lines = _FALLBACK_PLUGINS

    plugin_names   = []
    success_count  = 0
    total_scripts  = 0

    for raw_line in lines:
        script_name = raw_line.strip()
        if not script_name or not script_name.endswith(".lua"):
            continue
        total_scripts += 1

        # Klasör adı eşlemesi
        folder_map = {"wcsync.lua": "WCSync", "wchub.lua": "WCHub"}
        folder_name = folder_map.get(script_name, script_name[:-4])
        plugin_names.append(folder_name)

        try:
            req  = urllib.request.Request(f"{base_url}/{script_name}",
                                          headers={"Cache-Control": "no-cache"})
            code = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")

            if "{PORT}" in code:
                code = code.replace("{PORT}", str(HTTP_PORT))

            plugin_dir = pathlib.Path(server_dir) / "Plugins" / folder_name
            plugin_dir.mkdir(parents=True, exist_ok=True)

            (plugin_dir / "main.lua").write_text(code + "\n", encoding="utf-8")
            (plugin_dir / "Info.lua").write_text(
                f'g_PluginInfo = {{Name="{folder_name}", Version="1"}}\n',
                encoding="utf-8"
            )
            success_count += 1
        except Exception as e:
            log_msg(f"[GÜNCELLEME] {script_name} çekilemedi: {e}")

    # Listede olmayan eski eklentileri sil
    plugins_base = pathlib.Path(server_dir) / "Plugins"
    if plugins_base.exists():
        for item in plugins_base.iterdir():
            if item.is_dir() and item.name not in plugin_names:
                try:
                    shutil.rmtree(item)
                    log_msg(f"[TEMİZLİK] Eski eklenti silindi: {item.name}")
                except Exception as e:
                    log_msg(f"[TEMİZLİK] {item.name} silinemedi: {e}")

    plugins_ini = "\n".join(f"Plugin={n}" for n in plugin_names)
    settings_ini = f"""[Authentication]
Authenticate=0
OnlineMode=0
ServerID=WCHubEngine

[Plugins]
{plugins_ini}

[Server]
Description=Minecraft Distributed Hub
MaxPlayers=100
Port={CUBERITE_PORT}
Ports={CUBERITE_PORT}
NetworkCompressionThreshold=-1
"""
    try:
        (pathlib.Path(server_dir) / "settings.ini").write_text(
            settings_ini.strip() + "\n", encoding="utf-8"
        )
    except Exception as e:
        log_msg(f"[GÜNCELLEME] settings.ini yazılamadı: {e}")

    if success_count > 0:
        log_msg(f"[GÜNCELLEME] {success_count}/{total_scripts} eklenti senkronize edildi.")
        return True

    log_msg("[GÜNCELLEME] Hiçbir eklenti indirilemedi.")
    return False


# ══════════════════════════════════════════════════════════
#  MİNECRAFT PROTOKOL YARDIMCILARI
# ══════════════════════════════════════════════════════════

# BUG FIX #4 — VarInt fonksiyonlarına maksimum 5 byte limiti eklendi.
# Önceden: sonsuz while döngüsü → bozuk/kötü amaçlı paket → sonsuz CPU tüketimi.

_VARINT_MAX_BYTES = 5


def vi_enc(v: int) -> bytes:
    r = bytearray()
    v &= 0xFFFFFFFF  # 32-bit unsigned
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            b |= 0x80
        r.append(b)
        if not v:
            break
    return bytes(r)


def vi_dec(data: bytes, pos: int = 0):
    r = shift = 0
    for _ in range(_VARINT_MAX_BYTES):
        b     = data[pos]; pos += 1
        r    |= (b & 0x7F) << shift
        if not (b & 0x80):
            return r, pos
        shift += 7
    raise ValueError("VarInt aşımı (>5 byte)")


async def vi_rd(reader) -> int:
    r = shift = 0
    for _ in range(_VARINT_MAX_BYTES):
        b     = (await reader.readexactly(1))[0]
        r    |= (b & 0x7F) << shift
        if not (b & 0x80):
            return r
        shift += 7
    raise ValueError("VarInt aşımı (>5 byte)")


# BUG FIX #5 — pkt_read'e timeout parametresi eklendi.
# Önceden: yavaş / kötü amaçlı istemci bağlantıyı sonsuza dek bloke edebiliyordu.
_PACKET_READ_TIMEOUT = 120  # saniye


async def pkt_read(reader, comp: int = -1, timeout: float = _PACKET_READ_TIMEOUT):
    async def _read():
        length = await vi_rd(reader)
        raw    = await reader.readexactly(length)
        if comp < 0:
            pid, pos = vi_dec(raw)
            return pid, raw[pos:], vi_enc(length) + raw
        data_len, pos = vi_dec(raw)
        inner = raw[pos:]
        if data_len == 0:
            pid, p2 = vi_dec(inner)
            return pid, inner[p2:], vi_enc(length) + raw
        dec      = zlib.decompress(inner)
        pid, p2  = vi_dec(dec)
        return pid, dec[p2:], vi_enc(length) + raw

    return await asyncio.wait_for(_read(), timeout=timeout)


def pkt_make(pid: int, payload: bytes, comp: int = -1) -> bytes:
    data = vi_enc(pid) + payload
    if comp < 0:
        return vi_enc(len(data)) + data
    if len(data) < comp:
        inner = vi_enc(0) + data
        return vi_enc(len(inner)) + inner
    c     = zlib.compress(data)
    inner = vi_enc(len(data)) + c
    return vi_enc(len(inner)) + inner


def mc_str_enc(s: str) -> bytes:
    b = s.encode("utf-8")
    return vi_enc(len(b)) + b


def mc_str_dec(data: bytes, pos: int = 0):
    n, pos = vi_dec(data, pos)
    return data[pos:pos + n].decode("utf-8", errors="replace"), pos + n


# ══════════════════════════════════════════════════════════
#  PROXY: YÖNLENDİRİCİ
# ══════════════════════════════════════════════════════════

class PlayerConn:
    def __init__(self, cr, cw):
        self.client_r     = cr
        self.client_w     = cw
        self.server_r     = None
        self.server_w     = None
        self.comp         = -1
        self.username     = "?"
        self.current_label = ""
        self.play_state   = False
        self.is_swapping  = False

    def _safe_write(self, data: bytes) -> None:
        try:
            self.client_w.write(data)
        except Exception:
            pass

    async def _safe_drain(self) -> None:
        try:
            await self.client_w.drain()
        except Exception:
            pass

    async def get_target_server(self, requested_label: str = None):
        try:
            import aiosqlite
            now = int(time.time())
            async with aiosqlite.connect(DB_FILE) as db:
                db.row_factory = aiosqlite.Row
                if requested_label:
                    async with db.execute(
                        "SELECT * FROM servers WHERE label=?", (requested_label,)
                    ) as cur:
                        return await cur.fetchone()

                async with db.execute(
                    "SELECT last_server FROM players WHERE username=?", (self.username,)
                ) as cur:
                    p_row = await cur.fetchone()
                    if p_row and p_row["last_server"]:
                        async with db.execute(
                            "SELECT * FROM servers WHERE label=? AND (? - last_seen) < 60",
                            (p_row["last_server"], now)
                        ) as scur:
                            s_row = await scur.fetchone()
                            if s_row:
                                return s_row

                async with db.execute(
                    """SELECT * FROM servers
                       WHERE players < 100 AND (? - last_seen) < 60
                       ORDER BY players ASC LIMIT 1""",
                    (now,)
                ) as cur:
                    return await cur.fetchone()
        except Exception as e:
            log_msg(f"[PROXY] get_target_server hatası: {e}")
            return None

    async def hot_swap(self, target_label: str) -> None:
        if self.current_label == target_label or self.is_swapping:
            return
        self.is_swapping = True
        prev_play_state  = self.play_state
        self.play_state  = False

        try:
            msg = json.dumps({
                "text": f"§a{target_label} sunucusuna geçiliyor... Envanter senkronize ediliyor.",
                "color": "yellow"
            })
            self._safe_write(pkt_make(0x02, mc_str_enc(msg) + bytes([0]), self.comp))
            await self._safe_drain()

            # Eski backend bağlantısını kapat
            if self.server_w:
                with contextlib.suppress(Exception):
                    self.server_w.close()
                self.server_w = None
                self.server_r = None

            self.comp = -1
            await asyncio.sleep(2.5)

            srv = await self.get_target_server(target_label)
            if not srv:
                fail_msg = json.dumps({"text": f"§c{target_label} bulunamadı!", "color": "red"})
                self._safe_write(pkt_make(0x02, mc_str_enc(fail_msg) + bytes([0]), self.comp))
                await self._safe_drain()
                return

            self.server_r, self.server_w = await asyncio.wait_for(
                asyncio.open_connection(srv["host"], srv["port"], limit=2 ** 20),
                timeout=10
            )

            hs = (vi_enc(47) + mc_str_enc(srv["host"]) +
                  struct.pack(">H", srv["port"]) + vi_enc(2))
            self.server_w.write(pkt_make(0x00, hs, -1))
            self.server_w.write(pkt_make(0x00, mc_str_enc(self.username), -1))
            await self.server_w.drain()

            # Login Success / Join Game paketi bekle
            while True:
                pid, payload, raw = await pkt_read(self.server_r, self.comp, timeout=15)
                if pid == 0x01:
                    dim = payload[4]
                    respawn_fake = (struct.pack(">i", -1 if dim == 0 else 0)
                                   + payload[5:8] + mc_str_enc("default"))
                    respawn_real = (struct.pack(">i", dim)
                                   + payload[5:8] + mc_str_enc("default"))
                    self._safe_write(pkt_make(0x07, respawn_fake, self.comp))
                    self._safe_write(pkt_make(0x07, respawn_real, self.comp))

                    pos_data = struct.pack(">dddff", 0.0, 5.0, 0.0, 0.0, 0.0) + bytes([0])
                    self._safe_write(pkt_make(0x08, pos_data, self.comp))
                    await self._safe_drain()

                    self.current_label = target_label
                    self.play_state    = True

                    try:
                        import aiosqlite
                        async with aiosqlite.connect(DB_FILE) as db:
                            await db.execute(
                                "UPDATE players SET last_server=? WHERE username=?",
                                (target_label, self.username)
                            )
                            await db.commit()
                    except Exception as e:
                        log_msg(f"[SWAP] DB güncelleme hatası: {e}")

                    log_msg(f"[SWAP] {self.username} → {target_label}")
                    return
        except Exception as e:
            log_msg(f"[SWAP] {self.username} geçiş hatası ({target_label}): {e}")
            # BUG FIX #6 — Başarısız swap sonrası play_state düzgün geri alınıyor.
            self.play_state = prev_play_state
            # Tutarsız bağlantı durumunu temizle
            if self.server_w:
                with contextlib.suppress(Exception):
                    self.server_w.close()
                self.server_w = None
                self.server_r = None
        finally:
            self.is_swapping = False

    async def pipe_c2s(self) -> None:
        while True:
            if self.is_swapping or not self.server_w:
                await asyncio.sleep(0.05)
                continue
            try:
                pid, payload, raw = await pkt_read(self.client_r, self.comp)
                if pid == 0x01 and self.play_state:
                    msg, _ = mc_str_dec(payload)
                    if msg.startswith("/wc_transfer "):
                        target = msg.split(" ", 1)[1].strip()
                        if target:
                            # BUG FIX #7 — asyncio.ensure_future → asyncio.create_task
                            asyncio.create_task(self.hot_swap(target))
                        continue
                    elif not msg.startswith("/"):
                        formatted = json.dumps({
                            "text": f"§8[§b{self.current_label}§8] §7{self.username}§f: {msg}"
                        })
                        b_pkt = pkt_make(0x02, mc_str_enc(formatted) + bytes([0]), self.comp)
                        with _PLAYERS_LOCK:
                            players_snapshot = list(_active_players)
                        for c in players_snapshot:
                            if c.play_state:
                                with contextlib.suppress(Exception):
                                    c.client_w.write(b_pkt)
                        continue
                if self.server_w:
                    self.server_w.write(raw)
                    await self.server_w.drain()
            except Exception:
                if not self.is_swapping:
                    break

    async def pipe_s2c(self) -> None:
        while True:
            if self.is_swapping or not self.server_r:
                await asyncio.sleep(0.05)
                continue
            try:
                pid, payload, raw = await pkt_read(self.server_r, self.comp)
                if pid == 0x03 and self.comp < 0:
                    self.comp, _ = vi_dec(payload)
                self._safe_write(raw)
                await self._safe_drain()
            except Exception:
                if not self.is_swapping:
                    break

    async def run(self) -> None:
        try:
            pid, payload, raw = await pkt_read(self.client_r, -1, timeout=30)
            p = 0
            _, p = vi_dec(payload, p)
            _, p = mc_str_dec(payload, p)
            p += 2
            next_state, _ = vi_dec(payload, p)

            if next_state == 1:
                with _PLAYERS_LOCK:
                    online_count = len(_active_players)
                status_json = json.dumps({
                    "version":     {"name": "1.8.x", "protocol": 47},
                    "players":     {"max": 1000, "online": online_count, "sample": []},
                    "description": {"text": f"§bWC Merkezi Hub §8- §e{online_count} Aktif"}
                })
                self._safe_write(pkt_make(0x00, mc_str_enc(status_json), -1))
                await self._safe_drain()
                return

            if next_state == 2:
                pid2, payload2, raw2 = await pkt_read(self.client_r, -1, timeout=30)
                self.username, _ = mc_str_dec(payload2)

                srv = await self.get_target_server()
                if not srv:
                    self._safe_write(pkt_make(0x00, mc_str_enc(
                        json.dumps({"text": "§cSunucu bulunamadı veya hepsi kapalı."})
                    ), -1))
                    await self._safe_drain()
                    return

                self.current_label = srv["label"]
                self.server_r, self.server_w = await asyncio.wait_for(
                    asyncio.open_connection(srv["host"], srv["port"], limit=2 ** 20),
                    timeout=10
                )
                self.server_w.write(raw)
                self.server_w.write(raw2)
                await self.server_w.drain()

                with _PLAYERS_LOCK:
                    _active_players.append(self)
                self.play_state = True
                log_msg(f"[JOIN] {self.username} → {self.current_label}")

                try:
                    import aiosqlite
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute(
                            "INSERT OR IGNORE INTO players (username, last_server) VALUES (?, ?)",
                            (self.username, self.current_label)
                        )
                        await db.commit()
                except Exception as e:
                    log_msg(f"[JOIN] DB kayıt hatası: {e}")

                await asyncio.gather(self.pipe_s2c(), self.pipe_c2s())
        except Exception as e:
            log_msg(f"[PROXY] {self.username} bağlantı hatası: {e}")
        finally:
            with _PLAYERS_LOCK:
                if self in _active_players:
                    _active_players.remove(self)
            log_msg(f"[QUIT] {self.username} bağlantısı kesildi.")
            for w in (self.client_w, self.server_w):
                if w:
                    with contextlib.suppress(Exception):
                        w.close()


async def handle_player(cr, cw) -> None:
    await PlayerConn(cr, cw).run()


# ══════════════════════════════════════════════════════════
#  CUBERITE YÖNETİMİ
# ══════════════════════════════════════════════════════════

def _write_to_cuberite(cmd: str) -> None:
    """Cuberite stdin'ine thread-safe komut gönder."""
    with _STDIN_LOCK:
        if _cuberite_proc and _cuberite_proc.poll() is None:
            try:
                _cuberite_proc.stdin.write(cmd.strip() + "\n")
                _cuberite_proc.stdin.flush()
            except Exception as e:
                log_msg(f"[CUBERITE] stdin yazma hatası: {e}")


def _restart_local_cuberite() -> None:
    global _cuberite_proc
    if _cuberite_proc and _cuberite_proc.poll() is None:
        log_msg("[CUBERITE] Yeniden başlatma sinyali gönderiliyor...")
        try:
            _cuberite_proc.terminate()
            _cuberite_proc.wait(timeout=8)
        except Exception:
            try:
                _cuberite_proc.kill()
            except Exception:
                pass
        _cuberite_proc = None


def run_cuberite() -> None:
    update_and_configure()

    mc_bin = next(iter(glob.glob("/server/**/Cuberite", recursive=True)), None)
    if not mc_bin:
        # BUG FIX #8 — Cuberite bulunamadığında sessizce çıkmak yerine log basılıyor.
        log_msg("[CUBERITE] HATA: Cuberite ikili dosyası bulunamadı! /server altında aranıyor.")
        return

    os.chmod(mc_bin, 0o755)
    persistent_world = os.path.join(DATA_DIR, "world") if MODE == "all" else "/server/world"
    proxy_url        = os.environ.get("PROXY_URL", f"http://127.0.0.1:{HTTP_PORT}")

    def _pipe_output(stream, proc):
        for raw in stream:
            line = (raw.rstrip() if isinstance(raw, str)
                    else raw.decode("utf-8", "replace").rstrip())
            if not line:
                continue
            log_msg(f"[CUBERITE] {line}")

            if "WCSYNC_JOIN:" in line:
                def _do_join(ln):
                    try:
                        name, uuid_str = ln.split("WCSYNC_JOIN:")[1].strip().split(":")
                        uuid_clean = uuid_str.replace("-", "")
                        req  = urllib.request.Request(
                            f"{proxy_url}/api/player_file?name={name}"
                        )
                        data = urllib.request.urlopen(req, timeout=5).read()
                        for fname in (uuid_str, uuid_clean):
                            p = pathlib.Path(f"{persistent_world}/players/{fname}.json")
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_bytes(data)
                        time.sleep(1.0)
                        _write_to_cuberite(f"wcreload {name}")
                    except Exception as e:
                        log_msg(f"[WCSYNC-JOIN] {e}")
                threading.Thread(target=_do_join, args=(line,), daemon=True).start()

            elif "WCSYNC_QUIT:" in line:
                def _do_upload(ln):
                    try:
                        time.sleep(1.0)
                        name, uuid_str = ln.split("WCSYNC_QUIT:")[1].strip().split(":")
                        uuid_clean = uuid_str.replace("-", "")
                        p1 = pathlib.Path(f"{persistent_world}/players/{uuid_str}.json")
                        p2 = pathlib.Path(f"{persistent_world}/players/{uuid_clean}.json")
                        target_p = p1 if p1.exists() else (p2 if p2.exists() else None)
                        if target_p:
                            req = urllib.request.Request(
                                f"{proxy_url}/api/player_file?name={name}",
                                data=target_p.read_bytes(), method="POST"
                            )
                            urllib.request.urlopen(req, timeout=5)
                    except Exception as e:
                        log_msg(f"[WCSYNC-QUIT] {e}")
                threading.Thread(target=_do_upload, args=(line,), daemon=True).start()

    while True:
        global _cuberite_proc
        log_msg("[CUBERITE] Başlatılıyor...")
        try:
            proc = subprocess.Popen(
                [mc_bin],
                cwd=str(pathlib.Path(mc_bin).parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            _cuberite_proc = proc
            threading.Thread(target=_pipe_output, args=(proc.stdout, proc),
                             daemon=True).start()
            proc.wait()
        except Exception as e:
            log_msg(f"[CUBERITE] Süreç hatası: {e}")
        finally:
            _cuberite_proc = None

        log_msg("[CUBERITE] Süreç durdu. 5 saniye sonra yeniden başlatılacak...")
        time.sleep(5)


def register_local_cuberite() -> None:
    while True:
        time.sleep(15)
        try:
            payload = json.dumps({
                "host": "127.0.0.1",
                "port": CUBERITE_PORT,
                "server_id": "LOCAL_HUB_01"
            })
            req = urllib.request.Request(
                f"http://127.0.0.1:{HTTP_PORT}/api/register",
                data=payload.encode(),
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # HTTP server henüz hazır olmayabilir, sessiz devam et


# ══════════════════════════════════════════════════════════
#  BORE TÜNELLEME
# ══════════════════════════════════════════════════════════

def run_bore_for_proxy() -> None:
    global _proxy_bore_addr
    while True:
        try:
            proc = subprocess.Popen(
                ["bore", "local", str(MC_PORT), "--to", "bore.pub"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                line = re.sub(r'\x1b\[[0-9;]*[mK]|\x1b\[\d*[A-Za-z]|\x1b\(\w', '',
                              line.rstrip())
                if not line:
                    continue
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    _proxy_bore_addr = f"bore.pub:{m.group(1)}"
                    log_msg(f"[BORE] Proxy tüneli açıldı: {_proxy_bore_addr}")
            proc.wait()
        except Exception as e:
            log_msg(f"[BORE-PROXY] Hata: {e}")
        time.sleep(5)


def run_bore_for_gameserver() -> None:
    global _pending_remote_logs
    proxy_url = os.environ.get("PROXY_URL", "")
    if not proxy_url:
        return

    current_gs_bore = None

    # BUG FIX #9 — server_id dosyası with open() ile güvenli şekilde okunuyor/yazılıyor.
    server_id_file = os.path.join(DATA_DIR, "server_id.txt")
    if os.path.exists(server_id_file):
        with open(server_id_file, "r") as f:
            server_id = f.read().strip()
    else:
        server_id = str(uuid.uuid4())
        pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        with open(server_id_file, "w") as f:
            f.write(server_id)

    def heartbeat():
        while True:
            time.sleep(15)
            if not current_gs_bore:
                continue
            try:
                host, port_str = current_gs_bore.split(":")
                payload = json.dumps({
                    "host": host, "port": int(port_str), "server_id": server_id
                })
                req  = urllib.request.Request(
                    f"{proxy_url}/api/register",
                    data=payload.encode(),
                    headers={"Content-Type": "application/json"}
                )
                resp_data = json.loads(urllib.request.urlopen(req, timeout=5).read())
                if resp_data.get("restart"):
                    _restart_local_cuberite()
            except Exception:
                pass

    def sync_loop():
        nonlocal current_gs_bore
        global _pending_remote_logs
        last_cmd_id       = 0
        last_update_ts    = time.time()

        while True:
            time.sleep(1.5)
            with _LOG_LOCK:
                logs_to_send = list(_pending_remote_logs)
                _pending_remote_logs.clear()

            payload = {
                "server_id":      server_id,
                "logs":           logs_to_send,
                "last_cmd_id":    last_cmd_id,
                "last_update_ts": last_update_ts
            }
            try:
                req  = urllib.request.Request(
                    f"{proxy_url}/api/node_sync",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}
                )
                data = json.loads(urllib.request.urlopen(req, timeout=5).read())

                for cmd_obj in data.get("commands", []):
                    cid = cmd_obj["id"]
                    cmd = cmd_obj["cmd"]
                    if cid > last_cmd_id:
                        last_cmd_id = cid
                        _write_to_cuberite(cmd)

                with _SCRIPT_TS_LOCK:
                    latest_ts = _last_script_update
                if (data.get("update_scripts") and
                        data.get("current_ts", 0) > last_update_ts):
                    last_update_ts = data["current_ts"]
                    log_msg("[SYNC] Güncelleme sinyali alındı! GitHub'dan çekiliyor...")
                    update_and_configure()
                    _write_to_cuberite("reload")

            except Exception:
                # BUG FIX #10 — Başarısız istek sonrası loglar kaybolmuyordu;
                # yeni loglar gelmişse doğru sıraya ekleniyor.
                with _LOG_LOCK:
                    _pending_remote_logs = logs_to_send + _pending_remote_logs

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()

    while True:
        try:
            proc = subprocess.Popen(
                ["bore", "local", str(CUBERITE_PORT), "--to", "bore.pub"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                line = re.sub(r'\x1b\[[0-9;]*[mK]|\x1b\[\d*[A-Za-z]|\x1b\(\w', '',
                              line.rstrip())
                if not line:
                    continue
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    current_gs_bore = f"bore.pub:{m.group(1)}"
                    log_msg(f"[BORE] Alt sunucu tüneli: {current_gs_bore}")
            proc.wait()
        except Exception as e:
            log_msg(f"[BORE-GS] Hata: {e}")
        time.sleep(5)


# ══════════════════════════════════════════════════════════
#  HTTP API VE YÖNETİM PANELİ
# ══════════════════════════════════════════════════════════

_PANEL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WC Network Panel</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;min-height:100vh;padding:24px 24px 60px}}
h1{{font-size:1.6rem;margin-bottom:4px;color:#58a6ff}}
.subtitle{{color:#8b949e;font-size:.85rem;margin-bottom:24px}}
.cards{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:28px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px 24px;min-width:160px}}
.card .val{{font-size:2rem;font-weight:700;color:#58a6ff}}
.card .lbl{{font-size:.75rem;color:#8b949e;margin-top:4px}}
.addr{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin-bottom:24px;font-family:monospace;color:#3fb950;font-size:.9rem}}
.addr span{{color:#8b949e;font-size:.75rem;display:block;margin-bottom:4px}}
table{{width:100%;border-collapse:collapse;background:#161b22;border-radius:10px;overflow:hidden;border:1px solid #30363d}}
th{{background:#21262d;padding:12px 16px;text-align:left;font-size:.75rem;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}}
td{{padding:12px 16px;border-top:1px solid #21262d;font-size:.88rem}}
.badge{{background:#21262d;border-radius:4px;padding:2px 6px;font-size:.7rem;color:#8b949e}}
.players{{color:#3fb950;font-weight:600}}
.btn{{border:none;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:.8rem;font-weight:600;transition:.15s}}
.btn-danger{{background:#da3633;color:#fff}}.btn-danger:hover{{background:#f85149}}
.btn-warn{{background:#9e6a03;color:#fff}}.btn-warn:hover{{background:#d29922}}
.btn-success{{background:#238636;color:#fff}}.btn-success:hover{{background:#2ea043}}
.btn:disabled{{opacity:.4;cursor:not-allowed}}
.actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:20px}}
#toast{{position:fixed;bottom:24px;right:24px;background:#238636;color:#fff;padding:12px 20px;border-radius:8px;font-size:.85rem;display:none;z-index:99;border:1px solid #2ea043}}
#toast.err{{background:#da3633;border-color:#f85149}}
.section-title{{font-size:.8rem;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;margin:30px 0 10px}}
.console-box{{background:#000;border:1px solid #30363d;border-radius:8px;height:350px;overflow-y:auto;padding:12px;font-family:monospace;color:#3fb950;font-size:13px;line-height:1.4;margin-bottom:12px;white-space:pre-wrap;word-break:break-all}}
.console-input-row{{display:flex;gap:10px}}
.console-input{{flex:1;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px;color:#fff;font-family:monospace;outline:none}}
.console-input:focus{{border-color:#58a6ff}}
</style>
</head>
<body>
<h1>⛏️ WC Network Panel</h1>
<p class="subtitle">Minecraft Bungee Network Yönetim Paneli</p>
<div class="cards">
  <div class="card"><div class="val" id="statPlayers">{online}</div><div class="lbl">Aktif Oyuncu</div></div>
  <div class="card"><div class="val" id="statServers">{server_count}</div><div class="lbl">Aktif Sunucu</div></div>
  <div class="card"><div class="val">{net_status}</div><div class="lbl">Ağ Durumu</div></div>
</div>
<div class="addr"><span>Minecraft Bağlantı Adresi</span>{bore_addr}</div>
<div class="section-title">Sunucular</div>
<div class="actions">
  <button class="btn btn-danger" id="restartAllBtn" onclick="restartAll()">🔄 Tüm Sunucuları Yeniden Başlat</button>
  <button class="btn btn-success" id="updateScriptsBtn" onclick="updateScripts()">📥 Ağı Güncelle</button>
  <span id="statusMsg" style="color:#8b949e;font-size:.82rem"></span>
</div>
<table>
  <thead><tr><th>Sunucu</th><th>Adres</th><th>Oyuncu</th><th>İşlem</th></tr></thead>
  <tbody id="serverBody">{rows}</tbody>
</table>
<div class="section-title">🖥️ Canlı Konsol</div>
<div class="console-box" id="consoleBox">Yükleniyor...</div>
<div class="console-input-row">
  <input class="console-input" id="cmdInput" placeholder="Komut... (Örn: say Merhaba)">
  <button class="btn btn-warn" onclick="sendCommand()">Gönder</button>
</div>
<div id="toast"></div>
<script>
const cb=document.getElementById('consoleBox');
let autoScroll=true;
cb.addEventListener('scroll',()=>{{autoScroll=cb.scrollTop+cb.clientHeight>=cb.scrollHeight-20;}});
function toast(msg,err=false){{
  const t=document.getElementById('toast');
  t.textContent=msg;t.className=err?'err':'';t.style.display='block';
  setTimeout(()=>t.style.display='none',3500);
}}
async function restartAll(){{
  const btn=document.getElementById('restartAllBtn'),msg=document.getElementById('statusMsg');
  btn.disabled=true;msg.textContent='Sinyal gönderiliyor...';
  try{{const r=await fetch('/api/restart_all',{{method:'POST'}});const d=await r.json();
    toast('✅ '+d.message);msg.textContent='Gönderildi!';}}
  catch(e){{toast('❌ Hata: '+e,true);msg.textContent='';}}
  setTimeout(()=>{{btn.disabled=false;msg.textContent='';}},5000);
}}
async function restartOne(label){{
  if(!confirm(label+' yeniden başlatılsın mı?'))return;
  try{{const r=await fetch('/api/restart?label='+encodeURIComponent(label),{{method:'POST'}});
    const d=await r.json();toast('✅ '+d.message);}}
  catch(e){{toast('❌ Hata: '+e,true);}}
}}
async function updateScripts(){{
  const btn=document.getElementById('updateScriptsBtn');
  btn.disabled=true;toast('Güncelleniyor...');
  try{{const r=await fetch('/api/update_scripts',{{method:'POST'}});const d=await r.json();
    toast(d.ok?'✅ '+d.message:'❌ '+d.message,!d.ok);}}
  catch(e){{toast('❌ Hata: '+e,true);}}
  setTimeout(()=>{{btn.disabled=false;}},4000);
}}
async function fetchLogs(){{
  try{{const r=await fetch('/api/logs');const d=await r.json();
    const lines=d.logs.map(l=>l.replace(/</g,'&lt;').replace(/>/g,'&gt;'));
    cb.innerHTML=lines.join('<br>')||'Konsol geçmişi boş...';
    if(autoScroll)cb.scrollTop=cb.scrollHeight;}}
  catch(e){{}}
}}
setInterval(fetchLogs,1500);fetchLogs();
document.getElementById('cmdInput').addEventListener('keypress',e=>{{if(e.key==='Enter')sendCommand();}});
async function sendCommand(){{
  const input=document.getElementById('cmdInput');
  const cmd=input.value.trim();if(!cmd)return;input.value='';
  try{{await fetch('/api/command',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{command:cmd}})}});
    toast('✅ Komut gönderildi!');fetchLogs();}}
  catch(e){{toast('❌ Gönderim hatası',true);}}
}}
</script>
</body></html>"""


class HttpHandler(http.server.BaseHTTPRequestHandler):

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]

        # Ana panel
        if self.path == "/":
            self._serve_panel()
            return

        if self.path == "/api/logs":
            self._json_ok({"logs": list(SYSTEM_LOGS)})
            return

        if self.path == "/api/status":
            self._serve_status()
            return

        if path == "/api/player_file":
            self._serve_player_file()
            return

        if self.path == "/api/servers":
            self._serve_server_list()
            return

        self.send_response(404)
        self.end_headers()

    def _serve_panel(self):
        servers = []
        try:
            with _DB_LOCK, contextlib.closing(_sync_db_connect()) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT label, players, host, port FROM servers "
                    "WHERE (? - last_seen) < 45 ORDER BY label ASC",
                    (int(time.time()),)
                )
                servers = cur.fetchall()
        except Exception as e:
            log_msg(f"[WEB] Panel DB hatası: {e}")

        rows = ""
        for s in servers:
            badge = "🌐 HUB" if s["host"] == "127.0.0.1" else "🎮 GS"
            rows += (
                f"<tr>"
                f"<td><span class='badge'>{badge}</span> {s['label']}</td>"
                f"<td>{s['host']}:{s['port']}</td>"
                f"<td><span class='players'>👥 {s['players']}</span></td>"
                f"<td><button class='btn btn-warn' "
                f"onclick=\"restartOne('{s['label']}')\">🔄</button></td>"
                f"</tr>"
            )
        if not rows:
            rows = "<tr><td colspan='4' style='color:#8b949e;text-align:center;padding:28px'>Aktif sunucu yok</td></tr>"

        with _PLAYERS_LOCK:
            online = len(_active_players)

        html = _PANEL_HTML_TEMPLATE.format(
            online=online,
            server_count=len(servers),
            net_status="🟢" if servers else "🔴",
            bore_addr=_proxy_bore_addr or "Tünel bekleniyor...",
            rows=rows
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_status(self):
        servers = []
        try:
            with _DB_LOCK, contextlib.closing(_sync_db_connect()) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT label, players FROM servers "
                    "WHERE (? - last_seen) < 45 ORDER BY label ASC",
                    (int(time.time()),)
                )
                servers = [{"sunucu": r["label"], "oyuncu": r["players"]}
                           for r in cur.fetchall()]
        except Exception as e:
            log_msg(f"[WEB] Status DB hatası: {e}")

        with _PLAYERS_LOCK:
            online = len(_active_players)

        self._json_ok({
            "sistem":              "WC Bungee Network Aktif",
            "baglanti_adresi":     _proxy_bore_addr or "Tünel bekleniyor...",
            "toplam_oyuncu":       online,
            "aktif_sunucular":     servers
        })

    def _serve_player_file(self):
        name = self._safe_name()
        if not name:
            self.send_response(400); self.end_headers(); return
        filepath = pathlib.Path(DATA_DIR) / "players" / f"{name}.json"
        if filepath.exists():
            body = filepath.read_bytes()
            self.send_response(200); self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def _serve_server_list(self):
        if MODE == "gameserver":
            proxy_url = os.environ.get("PROXY_URL", "")
            if proxy_url:
                try:
                    req  = urllib.request.Request(f"{proxy_url}/api/servers")
                    resp = urllib.request.urlopen(req, timeout=15).read()
                    self.send_response(200); self.end_headers()
                    self.wfile.write(resp)
                except Exception:
                    self.send_response(502); self.end_headers()
            else:
                self.send_response(503); self.end_headers()
            return

        try:
            with _DB_LOCK, contextlib.closing(_sync_db_connect()) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT label, players FROM servers "
                    "WHERE (? - last_seen) < 45 ORDER BY label ASC",
                    (int(time.time()),)
                )
                resp = ";".join(f"{r['label']}:{r['players']}" for r in cur.fetchall())
            self.send_response(200); self.end_headers()
            self.wfile.write(resp.encode())
        except Exception as e:
            log_msg(f"[WEB] /api/servers hatası: {e}")
            self.send_response(500); self.end_headers()

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        global _last_script_update
        path = self.path.split("?")[0]

        if self.path == "/api/update_scripts":
            success = update_and_configure()
            if success:
                with _SCRIPT_TS_LOCK:
                    _last_script_update = time.time()
                _write_to_cuberite("reload")
                self._json_ok({"ok": True, "message": "Scriptler güncellendi ve ağa iletildi."})
            else:
                self._json_err(500, "Scriptler çekilirken hata oluştu.")
            return

        if self.path == "/api/command":
            body = self._read_json()
            if not body:
                self._json_err(400, "Geçersiz JSON"); return
            cmd = body.get("command", "").strip()
            # BUG FIX #11 — Komut sanitizasyonu: yalnızca güvenli karakterlere izin ver.
            if not cmd:
                self._json_err(400, "Komut boş olamaz"); return
            if not _CMD_SAFE_PATTERN.match(cmd):
                log_msg(f"[WEB] Güvensiz komut reddedildi: {cmd!r}")
                self._json_err(400, "Komut izin verilmeyen karakter içeriyor"); return
            with _CMD_LOCK:
                _cmd_counter.__class__  # noqa — sadece scope kontrolü
                globals()["_cmd_counter"] += 1
                cid = _cmd_counter
                _cmd_history.append({"id": cid, "cmd": cmd})
                if len(_cmd_history) > 200:
                    _cmd_history.pop(0)
            _write_to_cuberite(cmd)
            log_msg(f"[WEB-KOMUT] {cmd}")
            self._json_ok({"ok": True})
            return

        if self.path == "/api/node_sync":
            self._handle_node_sync(); return

        if self.path == "/api/restart_all":
            self._handle_restart_all(); return

        if path == "/api/restart":
            self._handle_restart_one(); return

        if path == "/api/player_file":
            self._handle_player_file_upload(); return

        if path == "/api/register":
            self._handle_register(); return

        self.send_response(404); self.end_headers()

    def _handle_node_sync(self):
        body = self._read_json()
        if not body:
            self.send_response(400); self.end_headers(); return
        srv_id = body.get("server_id", "UNKNOWN")
        label  = srv_id[:6]

        try:
            with _DB_LOCK, contextlib.closing(_sync_db_connect()) as conn:
                cur = conn.cursor()
                cur.execute("SELECT label FROM servers WHERE server_id=?", (srv_id,))
                row = cur.fetchone()
                if row:
                    label = row["label"]
        except Exception as e:
            log_msg(f"[SYNC] DB okuma hatası: {e}")

        for l in body.get("logs", []):
            clean = l.split("] ", 1)[-1] if "] " in l else l
            SYSTEM_LOGS.append(f"[{label}] {clean}")

        client_cmd_id    = body.get("last_cmd_id", 0)
        client_update_ts = body.get("last_update_ts", 0)

        with _CMD_LOCK:
            new_cmds = [c for c in _cmd_history if c["id"] > client_cmd_id]
        with _SCRIPT_TS_LOCK:
            latest_ts = _last_script_update

        resp = {
            "commands":       new_cmds,
            "update_scripts": latest_ts > client_update_ts,
            "current_ts":     latest_ts
        }
        self._json_ok(resp)

    def _handle_restart_all(self):
        count = 0
        try:
            with _DB_LOCK, contextlib.closing(_sync_db_connect()) as conn:
                conn.execute(
                    "UPDATE servers SET restart_pending=1 WHERE (? - last_seen) < 45",
                    (int(time.time()),)
                )
                count = conn.execute("SELECT changes()").fetchone()[0]
                conn.commit()
            _restart_local_cuberite()
            log_msg(f"[ADMIN] {count} sunucuya yeniden başlatma sinyali gönderildi.")
            self._json_ok({"ok": True, "message": f"{count} sunucuya sinyal gönderildi."})
        except Exception as e:
            log_msg(f"[ADMIN] restart_all hatası: {e}")
            self._json_err(500, str(e))

    def _handle_restart_one(self):
        label = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        ).get("label", [""])[0].strip()
        if not label:
            self._json_err(400, "label parametresi eksik"); return
        try:
            with _DB_LOCK, contextlib.closing(_sync_db_connect()) as conn:
                conn.execute(
                    "UPDATE servers SET restart_pending=1 WHERE label=?", (label,)
                )
                conn.commit()
            if label in ("LOCAL_HUB_01", "GM1") or MODE == "all":
                _restart_local_cuberite()
            log_msg(f"[ADMIN] '{label}' yeniden başlatma sinyali gönderildi.")
            self._json_ok({"ok": True, "message": f"'{label}' sinyali gönderildi."})
        except Exception as e:
            log_msg(f"[ADMIN] restart_one hatası: {e}")
            self._json_err(500, str(e))

    def _handle_player_file_upload(self):
        name = self._safe_name()
        if not name:
            self.send_response(400); self.end_headers(); return
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self.send_response(400); self.end_headers(); return
        data = self.rfile.read(length)
        try:
            dest = pathlib.Path(DATA_DIR) / "players" / f"{name}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            self.send_response(200); self.end_headers()
        except Exception as e:
            log_msg(f"[PLAYER-FILE] Kayıt hatası: {e}")
            self.send_response(500); self.end_headers()

    def _handle_register(self):
        body = self._read_json()
        if not body:
            self.send_response(400); self.end_headers(); return
        try:
            host      = body["host"]
            port      = int(body["port"])
            server_id = body.get("server_id")
            now       = int(time.time())

            with _DB_LOCK, contextlib.closing(_sync_db_connect()) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                restart_needed = False

                if server_id:
                    cur.execute(
                        "SELECT label, restart_pending FROM servers WHERE server_id=?",
                        (server_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        label          = row["label"]
                        restart_needed = bool(row["restart_pending"])
                        conn.execute(
                            "UPDATE servers SET host=?, port=?, last_seen=?, restart_pending=0 "
                            "WHERE label=?",
                            (host, port, now, label)
                        )
                    else:
                        cur.execute("SELECT COUNT(*) as cnt FROM servers")
                        label = f"GM{cur.fetchone()['cnt'] + 1}"
                        conn.execute(
                            "INSERT OR IGNORE INTO servers (label, server_id, host, port, last_seen) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (label, server_id, host, port, now)
                        )
                else:
                    cur.execute("SELECT COUNT(*) as cnt FROM servers")
                    label = f"GM{cur.fetchone()['cnt'] + 1}"
                    conn.execute(
                        "INSERT OR IGNORE INTO servers (label, host, port, last_seen) "
                        "VALUES (?, ?, ?, ?)",
                        (label, host, port, now)
                    )
                conn.commit()

            self._json_ok({"label": label, "restart": restart_needed})
            if restart_needed:
                log_msg(f"[REG] {label} için restart iletildi.")
        except Exception as e:
            log_msg(f"[REG] Kayıt hatası: {e}")
            self.send_response(500); self.end_headers()

    # ── Yardımcılar ──────────────────────────────────────────────────────────

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length))
        except Exception:
            return None

    def _safe_name(self) -> str:
        name = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        ).get("name", [""])[0]
        return "".join(c for c in name if c.isalnum() or c in "-_")

    def _json_ok(self, data: dict):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _json_err(self, code: int, message: str):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "message": message},
                                    ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass  # HTTP log çıktısını sustur


# ══════════════════════════════════════════════════════════
#  HTTP SUNUCU BAŞLATICI
# ══════════════════════════════════════════════════════════

def run_http() -> None:
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    # BUG FIX #12 — Tüm retry'lar tükenirse hata loglanıyor (önceden sessizdi).
    last_error = None
    for attempt in range(10):
        try:
            srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)
            log_msg(f"[HTTP] Yönetim paneli port {HTTP_PORT}'da hazır.")
            srv.serve_forever()
            return
        except OSError as e:
            last_error = e
            time.sleep(3)
    log_msg(f"[HTTP] KRITIK HATA: {HTTP_PORT} portunda başlatılamadı → {last_error}")


# ══════════════════════════════════════════════════════════
#  PROXY DÖNGÜSÜ
# ══════════════════════════════════════════════════════════

async def run_proxy() -> None:
    await init_db()
    server = await asyncio.start_server(handle_player, "0.0.0.0", MC_PORT)
    log_msg(f"[PROXY] Hub Yönlendirici port {MC_PORT}'da hazır.")
    async with server:
        await server.serve_forever()


# ══════════════════════════════════════════════════════════
#  ANA FONKSİYON
# ══════════════════════════════════════════════════════════

def main() -> None:
    log_msg(f"""
+--------------------------------------------------+
|  Minecraft Bungee Network & Anti-Dupe Engine     |
|  Mod: {MODE:<43}|
|  HTTP Port: {HTTP_PORT:<40}|
+--------------------------------------------------+""")

    if MODE == "proxy":
        threading.Thread(target=run_http,            daemon=True).start()
        threading.Thread(target=run_bore_for_proxy,  daemon=True).start()
        asyncio.run(run_proxy())

    elif MODE == "gameserver":
        threading.Thread(target=run_http,                daemon=True).start()
        threading.Thread(target=run_bore_for_gameserver, daemon=True).start()
        run_cuberite()

    elif MODE == "all":
        threading.Thread(target=run_http,               daemon=True).start()
        threading.Thread(target=run_bore_for_proxy,     daemon=True).start()
        threading.Thread(target=run_cuberite,           daemon=True).start()
        threading.Thread(target=register_local_cuberite, daemon=True).start()
        time.sleep(2)
        asyncio.run(run_proxy())

    else:
        log_msg(f"[MAIN] Bilinmeyen mod: {MODE!r}. Geçerli modlar: proxy, gameserver, all")
        sys.exit(1)


if __name__ == "__main__":
    main()
