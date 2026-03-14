#!/usr/bin/env python3
"""
⛏️  Minecraft Distributed World Engine  —  Tek Dosya
═══════════════════════════════════════════════════════════
  • MC 1.8 offline protokol MITM (şifresiz → tam kontrol)
  • Cross-server entity sync:
      - Farklı Cuberite'deki oyuncular birbirini gerçek
        oyuncu olarak GÖRÜR (isim etiketi dahil)
      - Birbirlerine SALDIRABILIR (PvP)
      - Blok değişiklikleri tüm sunucularda senkron
      - Chat tüm sunuculara iletilir
  • Oyuncu limiti: 999
  • Otomatik backend kaydı (wc-tsgd ana proxy)

  ENGINE_MODE=proxy      → Ana proxy (wc-tsgd)
  ENGINE_MODE=gameserver → Cuberite instance
  ENGINE_MODE=all        → Test: hepsi tek container
"""

import asyncio, json, os, pathlib, struct, sys
import threading, zlib, time, http.server, urllib.request
import subprocess, glob, uuid as _uuid_mod

MODE          = os.environ.get("ENGINE_MODE", "gameserver")
HTTP_PORT     = int(os.environ.get("PORT", 8080))
MC_PORT       = int(os.environ.get("MC_PORT", 25565))
DATA_DIR      = os.environ.get("DATA_DIR", "/data")
SERVER_DIR    = os.environ.get("SERVER_DIR", "/server")
BORE_FILE     = "/tmp/bore_address.txt"
STATE_FILE    = f"{DATA_DIR}/world_state.json"
BACKENDS_FILE = f"{DATA_DIR}/backends.json"

SETTINGS_INI = """
[Authentication]
Authenticate=0
OnlineMode=0
ServerID=CuberiteEngine
PlayerRestrictIP=0

[Server]
Description=Distributed World Engine
MaxPlayers=999
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
MaxPlayerInWorld=999

[Ranking]
DbFile=Ranking.sqlite

[RCON]
Enabled=0

[SlowSQL]
LogSlowQueries=0
""".strip()

WEBADMIN_INI = "[WebAdmin]\nEnabled=0\nPort=8081"

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
MaxViewDistance=10
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


def write_configs(server_dir=SERVER_DIR):
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
        except Exception as e:
            print(f"[CFG] HATA {path}: {e}")


# ══════════════════════════════════════════════════════════
#  MC 1.8 PROTOKOLü  (protocol 47, offline = sifresiz)
# ══════════════════════════════════════════════════════════

def vi_enc(v):
    r = bytearray()
    while True:
        b = v & 0x7F; v >>= 7
        if v: b |= 0x80
        r.append(b)
        if not v: break
    return bytes(r)

def vi_dec(data, pos=0):
    r = shift = 0
    while True:
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << shift
        if not (b & 0x80): return r, pos
        shift += 7
        if shift >= 35: raise ValueError("VarInt too long")

async def vi_rd(reader):
    r = shift = 0
    while True:
        b = (await reader.readexactly(1))[0]
        r |= (b & 0x7F) << shift
        if not (b & 0x80): return r
        shift += 7
        if shift >= 35: raise ValueError("VarInt too long")

async def pkt_read(reader, comp=-1):
    length = await vi_rd(reader)
    raw = await reader.readexactly(length)
    if comp < 0:
        pid, pos = vi_dec(raw)
        return pid, raw[pos:], vi_enc(length) + raw
    data_len, pos = vi_dec(raw)
    inner = raw[pos:]
    if data_len == 0:
        pid, p2 = vi_dec(inner)
        return pid, inner[p2:], vi_enc(length) + raw
    dec = zlib.decompress(inner)
    pid, p2 = vi_dec(dec)
    return pid, dec[p2:], vi_enc(length) + raw

def pkt_make(pid, payload, comp=-1):
    data = vi_enc(pid) + payload
    if comp < 0:
        return vi_enc(len(data)) + data
    if len(data) < comp:
        inner = vi_enc(0) + data
        return vi_enc(len(inner)) + inner
    c = zlib.compress(data)
    inner = vi_enc(len(data)) + c
    return vi_enc(len(inner)) + inner

def mc_str_enc(s):
    b = s.encode("utf-8")
    return vi_enc(len(b)) + b

def mc_str_dec(data, pos=0):
    n, pos = vi_dec(data, pos)
    return data[pos:pos+n].decode("utf-8", errors="replace"), pos+n

def pos_enc(x, y, z):
    v = ((x & 0x3FFFFFF) << 38) | ((y & 0xFFF) << 26) | (z & 0x3FFFFFF)
    return struct.pack(">q", v)

def pos_dec(data, pos=0):
    v = struct.unpack_from(">q", data, pos)[0]
    x = v >> 38; y = (v >> 26) & 0xFFF; z = v & 0x3FFFFFF
    if x >= (1 << 25): x -= (1 << 26)
    if z >= (1 << 25): z -= (1 << 26)
    return x, y, z, pos + 8

# Paket ID'leri (MC 1.8 / protocol 47)
PID_JOIN_GAME     = 0x01
PID_CHAT_SC       = 0x02
PID_UPDATE_HEALTH = 0x06
PID_POS_LOOK_SC   = 0x08
PID_SPAWN_PLAYER  = 0x0C
PID_DESTROY_ENT   = 0x13
PID_ENT_TELEPORT  = 0x18
PID_ENT_STATUS    = 0x1A
PID_MULTI_BLK     = 0x22
PID_BLOCK_CHG     = 0x23
PID_LOGIN_OK      = 0x02
PID_SET_COMP      = 0x03
PID_USE_ENTITY    = 0x02
PID_PLAYER_POS    = 0x04
PID_PLAYER_LOOK   = 0x05
PID_PLAYER_PL     = 0x06


# ══════════════════════════════════════════════════════════
#  PAYLAŞIMLI DÜNYA STATE
# ══════════════════════════════════════════════════════════

class WorldState:
    def __init__(self):
        self._lock  = threading.Lock()
        self.blocks = {}
        self._load()

    def record(self, x, y, z, bid):
        key = f"{x},{y},{z}"
        with self._lock:
            if bid == 0: self.blocks.pop(key, None)
            else:        self.blocks[key] = bid
        self._save()

    def record_multi(self, cx, cz, recs):
        with self._lock:
            for rx, ry, rz, bid in recs:
                key = f"{cx*16+rx},{ry},{cz*16+rz}"
                if bid == 0: self.blocks.pop(key, None)
                else:        self.blocks[key] = bid
        self._save()

    def replay_pkts(self, comp=-1):
        pkts = []
        with self._lock:
            items = list(self.blocks.items())
        for key, bid in items:
            try:
                x, y, z = map(int, key.split(","))
                pkts.append(pkt_make(PID_BLOCK_CHG, pos_enc(x,y,z)+vi_enc(bid), comp))
            except Exception: pass
        return pkts

    def _save(self):
        try:
            pathlib.Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(STATE_FILE).write_text(json.dumps({"blocks": self.blocks}))
        except Exception: pass

    def _load(self):
        try:
            self.blocks = json.loads(
                pathlib.Path(STATE_FILE).read_text()).get("blocks", {})
            print(f"[STATE] {len(self.blocks)} blok yuklendi.")
        except Exception: self.blocks = {}

world_state = WorldState()


# ══════════════════════════════════════════════════════════
#  CROSS-SERVER OYUNCU KAYITCISI
# ══════════════════════════════════════════════════════════

class PlayerInfo:
    __slots__ = ["username","uuid_str","x","y","z","yaw","pitch",
                 "health","real_eid","virtual_eid","conn"]
    def __init__(self, username, virtual_eid):
        self.username    = username
        self.uuid_str    = str(_uuid_mod.uuid3(
            _uuid_mod.UUID("00000000-0000-0000-0000-000000000000"),
            f"OfflinePlayer:{username}"))
        self.x = self.y = self.z = 0.0
        self.yaw = self.pitch    = 0.0
        self.health              = 20.0
        self.real_eid            = 0
        self.virtual_eid         = virtual_eid
        self.conn                = None


class CrossServerState:
    def __init__(self):
        self._lock    = threading.Lock()
        self._players = {}   # username -> PlayerInfo
        self._veid    = {}   # virtual_eid -> username
        self._counter = 100_000

    def register(self, username, conn):
        with self._lock:
            if username in self._players:
                info = self._players[username]
            else:
                info = PlayerInfo(username, self._counter)
                self._counter += 1
                self._players[username] = info
                self._veid[info.virtual_eid] = username
            info.conn = conn
        return info

    def unregister(self, username):
        with self._lock:
            info = self._players.pop(username, None)
            if info: self._veid.pop(info.virtual_eid, None)
        return info

    def by_veid(self, veid):
        with self._lock:
            name = self._veid.get(veid)
            return self._players.get(name) if name else None

    def update_pos(self, username, x=None, y=None, z=None, yaw=None, pitch=None):
        with self._lock:
            info = self._players.get(username)
            if not info: return
            if x     is not None: info.x     = x
            if y     is not None: info.y     = y
            if z     is not None: info.z     = z
            if yaw   is not None: info.yaw   = yaw
            if pitch is not None: info.pitch = pitch

cs_state = CrossServerState()


# -- Paket builder'lar ----------------------------------------

def _ang(deg):
    return struct.pack("B", int(deg / 360.0 * 256) & 0xFF)

def _fp(coord):
    return struct.pack(">i", int(coord * 32))

def pkt_spawn_player(info, comp=-1):
    payload = (
        vi_enc(info.virtual_eid) +
        mc_str_enc(info.uuid_str) +
        mc_str_enc(info.username) +
        vi_enc(0) +
        _fp(info.x) + _fp(info.y) + _fp(info.z) +
        _ang(info.yaw) + _ang(info.pitch) +
        struct.pack(">h", 0) +
        bytes([0x7F])
    )
    return pkt_make(PID_SPAWN_PLAYER, payload, comp)

def pkt_entity_teleport(info, comp=-1):
    payload = (
        vi_enc(info.virtual_eid) +
        _fp(info.x) + _fp(info.y) + _fp(info.z) +
        _ang(info.yaw) + _ang(info.pitch) +
        bytes([1])
    )
    return pkt_make(PID_ENT_TELEPORT, payload, comp)

def pkt_destroy_entity(veid, comp=-1):
    return pkt_make(PID_DESTROY_ENT, vi_enc(1) + vi_enc(veid), comp)

def pkt_entity_hurt(eid, comp=-1):
    # Entity Status: Entity ID = Int (4 byte, NOT VarInt)
    return pkt_make(PID_ENT_STATUS, struct.pack(">i", eid) + bytes([2]), comp)

def pkt_update_health(hp, comp=-1):
    return pkt_make(PID_UPDATE_HEALTH,
        struct.pack(">f", max(0.0, hp)) + vi_enc(20) + struct.pack(">f", 5.0), comp)

def pkt_chat_msg(text, color="yellow", comp=-1):
    return pkt_make(PID_CHAT_SC,
        mc_str_enc(json.dumps({"text": text, "color": color})) + bytes([0]), comp)


# ══════════════════════════════════════════════════════════
#  AKTIF BAGLANTILAR + BROADCAST
# ══════════════════════════════════════════════════════════

_active_lock = asyncio.Lock()
_active = []


def _cross_peers(conn):
    try:
        snap = list(_active)
    except Exception:
        return []
    return [
        c for c in snap
        if c is not conn
        and c.play_state
        and c.cs_info is not None
        and (c.backend_host != conn.backend_host
             or c.backend_port != conn.backend_port)
    ]


async def bcast_spawn(new_conn):
    info = new_conn.cs_info
    if not info: return
    peers = _cross_peers(new_conn)
    for c in peers:
        if c.cs_info:
            try: new_conn.client_w.write(pkt_spawn_player(c.cs_info, new_conn.comp))
            except Exception: pass
    if peers:
        try: await new_conn.client_w.drain()
        except Exception: pass
    for c in peers:
        try:
            c.client_w.write(pkt_spawn_player(info, c.comp))
            await c.client_w.drain()
        except Exception: pass


async def bcast_move(mover):
    info = mover.cs_info
    if not info: return
    peers = _cross_peers(mover)
    for c in peers:
        try:
            c.client_w.write(pkt_entity_teleport(info, c.comp))
            await c.client_w.drain()
        except Exception: pass


async def bcast_despawn(leaver):
    info = leaver.cs_info
    if not info: return
    peers = _cross_peers(leaver)
    for c in peers:
        try:
            c.client_w.write(pkt_destroy_entity(info.virtual_eid, c.comp))
            await c.client_w.drain()
        except Exception: pass


async def bcast_chat(sender, msg):
    peers = _cross_peers(sender)
    for c in peers:
        try:
            c.client_w.write(pkt_chat_msg(msg, "white", c.comp))
            await c.client_w.drain()
        except Exception: pass


# ══════════════════════════════════════════════════════════
#  BACKEND YONETIMI
# ══════════════════════════════════════════════════════════

def load_backends():
    try: return json.loads(pathlib.Path(BACKENDS_FILE).read_text())
    except Exception: return []

def save_backends(b):
    pathlib.Path(BACKENDS_FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(BACKENDS_FILE).write_text(json.dumps(b, indent=2))

def _cleanup_stale_backends():
    """90 saniyedir heartbeat gelmeyen backend'leri sil."""
    while True:
        time.sleep(30)
        try:
            backends = load_backends()
            now = time.time()
            alive = [b for b in backends
                     if now - b.get("last_seen", now) < 90]
            if len(alive) != len(backends):
                removed = len(backends) - len(alive)
                save_backends(alive)
                print(f"[CLEAN] {removed} eski backend silindi.")
        except Exception as e:
            print(f"[CLEAN] hata: {e}")

def pick_backend():
    backends = load_backends()
    if not backends: return None
    counts = {}
    for c in list(_active):
        k = f"{c.backend_host}:{c.backend_port}"
        counts[k] = counts.get(k, 0) + 1
    for b in sorted(backends,
                    key=lambda x: counts.get(f"{x['host']}:{x['port']}", 0),
                    reverse=True):
        if counts.get(f"{b['host']}:{b['port']}", 0) < 998:
            return b
    return backends[0]


# ══════════════════════════════════════════════════════════
#  OYUNCU BAGLANTISI  (MITM Proxy)
# ══════════════════════════════════════════════════════════

class PlayerConn:
    def __init__(self, cr, cw):
        self.client_r      = cr
        self.client_w      = cw
        self.server_r      = None
        self.server_w      = None
        self.state         = "handshake"
        self.play_state    = False
        self.comp          = -1   # her iki yon ayni threshold kullanir
        self.username      = "?"
        self.backend_host  = ""
        self.backend_port  = 0
        self.peer          = cw.get_extra_info("peername", ("?", 0))[0]
        self._replay_sent  = False
        self._spawned      = False
        self.cs_info       = None

    @property
    def s2c_comp(self): return self.comp
    @property
    def c2s_comp(self): return self.comp

    async def connect_backend(self, b):
        self.backend_host = b["host"]
        self.backend_port = b["port"]
        self.server_r, self.server_w = await asyncio.open_connection(
            b["host"], b["port"], limit=2**20)

    async def pipe_s2c(self):
        try:
            while True:
                pid, payload, raw = await pkt_read(self.server_r, self.comp)

                # Login
                if self.state == "login":
                    if pid == PID_SET_COMP:
                        # Her iki yon icin de ayni esik
                        self.comp, _ = vi_dec(payload)
                    elif pid == PID_LOGIN_OK:
                        self.state = "play"
                    self.client_w.write(raw)
                    await self.client_w.drain()
                    continue

                # Play: Join Game -> gercek entity ID'yi al
                if pid == PID_JOIN_GAME and self.cs_info and len(payload) >= 4:
                    try: self.cs_info.real_eid = struct.unpack_from(">i", payload, 0)[0]
                    except Exception: pass

                # Blok degisiklikleri -> world_state'e kaydet
                elif pid == PID_BLOCK_CHG and len(payload) >= 9:
                    try:
                        x, y, z, p2 = pos_dec(payload)
                        bid, _ = vi_dec(payload, p2)
                        world_state.record(x, y, z, bid)
                    except Exception: pass

                elif pid == PID_MULTI_BLK and len(payload) >= 8:
                    try:
                        cx = struct.unpack_from(">i", payload, 0)[0]
                        cz = struct.unpack_from(">i", payload, 4)[0]
                        count, p2 = vi_dec(payload, 8)
                        recs = []
                        for _ in range(count):
                            hp = payload[p2]; p2 += 1
                            ry = payload[p2]; p2 += 1
                            bid, p2 = vi_dec(payload, p2)
                            recs.append(((hp >> 4) & 0xF, ry, hp & 0xF, bid))
                        world_state.record_multi(cx, cz, recs)
                    except Exception: pass

                # Oyuncu yuklendi -> blok replay + cross-server spawn
                elif pid == PID_POS_LOOK_SC and not self._replay_sent:
                    self._replay_sent = True
                    self.client_w.write(raw)
                    await self.client_w.drain()
                    for p in world_state.replay_pkts(self.comp):
                        self.client_w.write(p)
                    await self.client_w.drain()
                    if not self._spawned and self.cs_info:
                        self._spawned = True
                        self.play_state = True
                        asyncio.ensure_future(bcast_spawn(self))
                    continue

                self.client_w.write(raw)
                await self.client_w.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError): pass
        except Exception as e: print(f"[S->C] {self.username}: {e}")
        finally: await self._cleanup()

    async def pipe_c2s(self):
        try:
            while True:
                pid, payload, raw = await pkt_read(self.client_r, self.comp)

                # Handshake
                if self.state == "handshake":
                    if pid == 0x00:
                        try:
                            p = 0
                            _, p = vi_dec(payload, p)
                            _, p = mc_str_dec(payload, p)
                            p += 2
                            ns, _ = vi_dec(payload, p)
                            if ns == 2: self.state = "login"
                        except Exception: pass
                    self.server_w.write(raw); await self.server_w.drain()
                    continue

                # Login
                if self.state == "login":
                    if pid == 0x00:
                        try:
                            self.username, _ = mc_str_dec(payload)
                            self.cs_info = cs_state.register(self.username, self)
                            print(f"[JOIN] {self.username} -> "
                                  f"{self.backend_host}:{self.backend_port}")
                        except Exception: pass
                    self.server_w.write(raw); await self.server_w.drain()
                    continue

                # Play

                # Pozisyon (C->S 0x04): X(d) Y(d) Z(d) OnGround(b) = 25 byte
                if pid == PID_PLAYER_POS and len(payload) >= 25:
                    try:
                        x, y, z = struct.unpack_from(">ddd", payload, 0)
                        cs_state.update_pos(self.username, x=x, y=y, z=z)
                        if self._spawned:
                            asyncio.ensure_future(bcast_move(self))
                    except Exception: pass

                # Bakis (C->S 0x05): Yaw(f) Pitch(f) OnGround(b) = 9 byte
                elif pid == PID_PLAYER_LOOK and len(payload) >= 9:
                    try:
                        yaw, pitch = struct.unpack_from(">ff", payload, 0)
                        cs_state.update_pos(self.username, yaw=yaw, pitch=pitch)
                    except Exception: pass

                # Pozisyon+Bakis (C->S 0x06): X Y Z (d*3=24) Yaw Pitch (f*2=8) OnGround = 33
                elif pid == PID_PLAYER_PL and len(payload) >= 33:
                    try:
                        x, y, z = struct.unpack_from(">ddd", payload, 0)
                        yaw, pitch = struct.unpack_from(">ff", payload, 24)
                        cs_state.update_pos(self.username, x=x, y=y, z=z,
                                            yaw=yaw, pitch=pitch)
                        if self._spawned:
                            asyncio.ensure_future(bcast_move(self))
                    except Exception: pass

                # Saldiri (C->S 0x02 Use Entity): type=1 saldiri
                elif pid == PID_USE_ENTITY:
                    try:
                        target_veid, p2 = vi_dec(payload, 0)
                        action, _       = vi_dec(payload, p2)
                        if action == 1:
                            target = cs_state.by_veid(target_veid)
                            if target and target.conn and target.conn is not self:
                                asyncio.ensure_future(self._cross_attack(target))
                                continue   # sunucuya iletme
                    except Exception: pass

                # Chat (C->S 0x01)
                elif pid == 0x01 and self.play_state:
                    try:
                        msg, _ = mc_str_dec(payload)
                        asyncio.ensure_future(
                            bcast_chat(self, f"[{self.username}] {msg}"))
                    except Exception: pass

                self.server_w.write(raw); await self.server_w.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError): pass
        except Exception as e: print(f"[C->S] {self.username}: {e}")
        finally: await self._cleanup()

    async def _cross_attack(self, target):
        DMG = 2.0
        target.health = max(0.0, target.health - DMG)
        tc = target.conn
        if not tc or not tc.cs_info: return

        # Hedefe: hasar animasyonu (gercek eid) + yeni can
        try:
            tc.client_w.write(pkt_entity_hurt(tc.cs_info.real_eid, tc.comp))
            tc.client_w.write(pkt_update_health(target.health, tc.comp))
            await tc.client_w.drain()
        except Exception: pass

        # Saldirganin ekraninda: hedefin hasar animasyonu (sanal eid)
        try:
            self.client_w.write(pkt_entity_hurt(target.virtual_eid, self.comp))
            await self.client_w.drain()
        except Exception: pass

        print(f"[PVP] {self.username} -> {target.username} ({target.health:.0f}/20 HP)")

        if target.health <= 0.0:
            target.health = 20.0
            msg = f"[{target.username} oldu! Olduran: {self.username}]"
            try:
                tc.client_w.write(pkt_update_health(20.0, tc.comp))
                tc.client_w.write(pkt_chat_msg(msg, "red", tc.comp))
                await tc.client_w.drain()
            except Exception: pass
            try:
                self.client_w.write(pkt_chat_msg(msg, "red", self.comp))
                await self.client_w.drain()
            except Exception: pass

    async def _cleanup(self):
        if self.cs_info:
            asyncio.ensure_future(bcast_despawn(self))
            cs_state.unregister(self.username)
            self.cs_info = None
        async with _active_lock:
            if self in _active:
                _active.remove(self)
                print(f"[QUIT] {self.username} ({len(_active)} aktif)")
        for w in (self.client_w, self.server_w):
            if w:
                try: w.close()
                except Exception: pass

    async def run(self):
        backends = load_backends()
        if not backends:
            print(f"[WARN] Backend yok! {self.peer}")
            self.client_w.close(); return

        # Player count bazlı sırala (en kalabalık önce)
        counts = {}
        for c in list(_active):
            k = f"{c.backend_host}:{c.backend_port}"
            counts[k] = counts.get(k, 0) + 1
        ordered = sorted(backends,
                         key=lambda x: counts.get(f"{x['host']}:{x['port']}", 0),
                         reverse=True)

        connected = False
        for b in ordered:
            if counts.get(f"{b['host']}:{b['port']}", 0) >= 998:
                continue
            try:
                await self.connect_backend(b)
                connected = True
                break
            except Exception as e:
                print(f"[ERR] Backend {b['host']}:{b['port']} basarisiz: {e}")

        if not connected:
            print(f"[WARN] Tum backendler basarisiz! {self.peer}")
            self.client_w.close(); return

        async with _active_lock:
            _active.append(self)
        print(f"[CONN] {self.peer} -> {self.backend_host}:{self.backend_port} ({len(_active)} aktif)")
        await asyncio.gather(self.pipe_s2c(), self.pipe_c2s())


async def handle_player(cr, cw):
    await PlayerConn(cr, cw).run()


# ══════════════════════════════════════════════════════════
#  HTTP DURUM SAYFASI
# ══════════════════════════════════════════════════════════

HTML = """\
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>Minecraft Engine</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#1a1a2e;color:#e0e0e0;font-family:'Courier New',monospace;
          display:flex;align-items:center;justify-content:center;
          min-height:100vh;padding:20px}}
    .card{{background:#16213e;border:2px solid #0f3460;border-radius:12px;
           padding:36px;max-width:740px;width:100%;text-align:center;
           box-shadow:0 0 30px rgba(15,52,96,.5)}}
    h1{{font-size:1.8rem;color:#4ecca3;margin-bottom:6px}}
    .sub{{color:#888;margin-bottom:20px;font-size:.85rem}}
    .addr{{background:#0f3460;border:1px solid #4ecca3;border-radius:8px;
           padding:14px;margin:14px 0;font-size:1.2rem;color:#4ecca3;word-break:break-all}}
    .stats{{display:flex;gap:12px;justify-content:center;margin:12px 0;flex-wrap:wrap}}
    .stat-box{{background:#0f3460;border:1px solid #4ecca355;border-radius:8px;
               padding:10px 18px;font-size:.9rem}}
    .stat-box b{{color:#4ecca3;font-size:1.1rem}}
    table{{width:100%;border-collapse:collapse;margin:14px 0;text-align:left}}
    th{{color:#4ecca3;border-bottom:1px solid #0f3460;padding:8px 10px;font-size:.8rem}}
    td{{padding:8px 10px;font-size:.88rem;border-bottom:1px solid #0f346033}}
    .on{{color:#4ecca3}}
    .dot{{display:inline-block;width:8px;height:8px;background:#4ecca3;
          border-radius:50%;margin-right:5px;animation:pulse 1.5s infinite}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .badge{{display:inline-block;background:#0f3460;border:1px solid #4ecca3;
            color:#4ecca3;font-size:.72rem;padding:2px 8px;border-radius:20px;margin:3px}}
    .info{{font-size:.75rem;color:#555;margin-top:14px}}
  </style>
</head>
<body>
<div class="card">
  <h1>Minecraft Distributed Engine</h1>
  <p class="sub">wc-yccy.onrender.com &bull; Ana Proxy &bull; bore.pub Tunnel</p>
  {addr_block}
  <div class="stats">
    <div class="stat-box">Toplam Oyuncu<br><b>{player_count}</b></div>
    <div class="stat-box">Blok Degisikligi<br><b>{block_count}</b></div>
    <div class="stat-box">Game Server<br><b>{server_count}</b></div>
  </div>
  <table>
    <tr><th>Game Server</th><th>Oyuncu</th><th>Durum</th></tr>
    {rows}
  </table>
  <div style="margin-top:14px">
    <span class="badge">Ultra Hafif</span>
    <span class="badge">Crack Girisi</span>
    <span class="badge">Ortak Dunya</span>
    <span class="badge">Cross-Server Chat</span>
    <span class="badge">Cross-Server PvP</span>
    <span class="badge">Blok Senkron</span>
    <span class="badge">999 Oyuncu</span>
  </div>
  <div class="info">Her 5 saniyede otomatik yenilenir</div>
</div></body></html>"""


def _build_html():
    try:
        bore = pathlib.Path(BORE_FILE).read_text().strip()
        addr_block = (f'<div class="addr">{bore}</div>'
                      f'<p style="color:#aaa;font-size:.85rem">'
                      f'Minecraft → Sunucu Ekle → bu adresi gir</p>')
    except Exception:
        addr_block = '<p style="color:#f8b400">Tunnel baslatiliyor...</p>'

    if MODE == "gameserver":
        label = os.environ.get("SERVER_LABEL", "GameServer")
        proxy = os.environ.get("PROXY_URL", "—")
        body  = f"""
          <p style="color:#4ecca3;font-size:1rem;margin:12px 0">
            ✅ GameServer aktif — <b>{label}</b></p>
          <p style="color:#aaa;font-size:.85rem">
            Oyuncular proxy üzerinden bağlanır: <b>{proxy}</b></p>
          <p style="color:#aaa;font-size:.85rem;margin-top:8px">
            Bu adres sadece gameserver yönetim sayfasıdır.</p>"""
        return HTML.format(
            addr_block=addr_block,
            player_count="—",
            block_count="—",
            server_count="1",
            rows=body,
        )

    backends = load_backends()
    counts = {}
    for c in list(_active):
        k = f"{c.backend_host}:{c.backend_port}"
        counts[k] = counts.get(k, 0) + 1

    rows = ""
    for b in backends:
        k     = f"{b['host']}:{b['port']}"
        n     = counts.get(k, 0)
        label = b.get("label", k)
        rows += (f'<tr><td>{label}</td><td>{n} oyuncu</td>'
                 f'<td><span class="dot"></span><span class="on">Aktif</span></td></tr>')
    if not rows:
        rows = '<tr><td colspan="3" style="color:#888;text-align:center">Game server bekleniyor...</td></tr>'

    return HTML.format(
        addr_block=addr_block,
        player_count=len(_active),
        block_count=len(world_state.blocks),
        server_count=len(backends),
        rows=rows,
    )


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = _build_html().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try: data = json.loads(self.rfile.read(length))
        except Exception: self._r(400, "bad json"); return

        if self.path == "/api/register":
            host  = data.get("host"); port = data.get("port")
            label = data.get("label", f"{host}:{port}")
            if not host or not port: self._r(400, "missing host/port"); return
            backends = load_backends()
            now = time.time()
            found = False
            # Önce label ile ara (bore port değişmiş olabilir)
            for b in backends:
                if b.get("label") == label:
                    b["host"] = host
                    b["port"] = int(port)
                    b["last_seen"] = now
                    found = True; break
            # Label yoksa host:port ile ara
            if not found:
                for b in backends:
                    if f"{b['host']}:{b['port']}" == f"{host}:{port}":
                        b["label"] = label
                        b["last_seen"] = now
                        found = True; break
            if not found:
                backends.append({"host": host, "port": int(port),
                                 "label": label, "last_seen": now})
            save_backends(backends)
            print(f"[REG] {label} ({host}:{port})")
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


# ══════════════════════════════════════════════════════════
#  BORE TUNNEL
# ══════════════════════════════════════════════════════════

def run_bore(port=MC_PORT):
    import re
    while True:
        try:
            pathlib.Path(BORE_FILE).unlink(missing_ok=True)
            proc = subprocess.Popen(
                ["bore", "local", str(port), "--to", "bore.pub"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = line.rstrip()
                print(f"[BORE] {line}")
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    addr = f"bore.pub:{m.group(1)}"
                    pathlib.Path(BORE_FILE).write_text(addr)
                    if MODE == "gameserver":
                        _register_with_proxy(addr)
            proc.wait()
        except FileNotFoundError:
            print("[BORE] bore bulunamadi, 10sn bekleniyor...")
        except Exception as e:
            print(f"[BORE] hata: {e}")
        time.sleep(10)


def _heartbeat_loop():
    """Gameserver modunda 30sn'de bir proxy'ye kayıt yenile."""
    while True:
        time.sleep(30)
        try:
            addr = pathlib.Path(BORE_FILE).read_text().strip()
            if addr:
                _register_with_proxy(addr)
        except Exception:
            pass


def _register_with_proxy(bore_addr):
    proxy_url = os.environ.get("PROXY_URL", "")
    if not proxy_url: return
    label = os.environ.get("SERVER_LABEL", "GameServer")
    host, port_str = bore_addr.split(":")
    try:
        body = json.dumps({"host": host, "port": int(port_str), "label": label}).encode()
        req  = urllib.request.Request(
            f"{proxy_url}/api/register", data=body,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        print(f"[REG] Proxy kayit: {proxy_url} ({label})")
    except Exception as e:
        print(f"[REG] Kayit hatasi: {e}")


# ══════════════════════════════════════════════════════════
#  CUBERITE BASLATICI
# ══════════════════════════════════════════════════════════

def run_cuberite():
    write_configs()
    flag = "/server/world/.initialized"
    if not pathlib.Path(flag).exists():
        for wp in ["/server/world", "/server/Server/world"]:
            if pathlib.Path(wp).exists():
                import shutil
                for sub in ["regions", "nether", "end"]:
                    shutil.rmtree(f"{wp}/{sub}", ignore_errors=True)
                for f in glob.glob(f"{wp}/*.mca") + glob.glob(f"{wp}/*.mcr"):
                    pathlib.Path(f).unlink(missing_ok=True)
        pathlib.Path("/server/world").mkdir(parents=True, exist_ok=True)
        pathlib.Path(flag).touch()
        print("[MC] Ilk baslatma: eski dunya temizlendi.")

    mc_bin = next(iter(glob.glob("/server/**/Cuberite", recursive=True)), None)
    if not mc_bin: print("[MC] HATA: Cuberite bulunamadi!"); return
    mc_dir = str(pathlib.Path(mc_bin).parent)
    os.chmod(mc_bin, 0o755)
    fifo = "/tmp/mc_stdin"
    pathlib.Path(fifo).unlink(missing_ok=True)
    os.mkfifo(fifo)
    subprocess.Popen(["tail", "-f", "/dev/null"],
                     stdout=open(fifo, "wb"), stderr=subprocess.DEVNULL)
    while True:
        proc = subprocess.Popen([mc_bin], cwd=mc_dir, stdin=open(fifo, "rb"))
        proc.wait()
        print("[MC] Cuberite kapandi, 5sn sonra yeniden baslatiliyor...")
        time.sleep(5)


# ══════════════════════════════════════════════════════════
#  ASYNC PROXY
# ══════════════════════════════════════════════════════════

async def run_proxy_async():
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    if not pathlib.Path(BACKENDS_FILE).exists():
        save_backends([])
    server = await asyncio.start_server(handle_player, "0.0.0.0", MC_PORT, limit=2**20)
    print(f"[PROXY] Port {MC_PORT} - Cross-server entity sync + PvP AKTIF")
    async with server:
        await server.serve_forever()


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def main():
    print(f"""
+--------------------------------------------------+
|  Minecraft Distributed World Engine              |
|  Mod : {MODE:<42}|
|  Cross-server: Entity + PvP + Chat + Blok        |
+--------------------------------------------------+""")

    if MODE == "config":
        write_configs(); return

    threading.Thread(target=run_http, daemon=True).start()

    if MODE == "proxy":
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        threading.Thread(target=_cleanup_stale_backends, daemon=True).start()
        asyncio.run(run_proxy_async())

    elif MODE == "gameserver":
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        threading.Thread(target=_heartbeat_loop, daemon=True).start()
        run_cuberite()

    elif MODE == "all":
        threading.Thread(target=run_bore, args=(MC_PORT,), daemon=True).start()
        threading.Thread(target=run_cuberite, daemon=True).start()
        time.sleep(3)
        save_backends([{"host": "127.0.0.1", "port": MC_PORT, "label": "LocalCuberite"}])
        asyncio.run(run_proxy_async())

    elif MODE == "http":
        run_http()

    else:
        print(f"Bilinmeyen mod: {MODE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
