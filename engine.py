#!/usr/bin/env python3
"""
⛏️  Minecraft Distributed Hub Engine  —  Tek Dosya (MAX OPTIMIZATION)
═══════════════════════════════════════════════════════════
  • Tam BungeeCord Mimarisi (Hot-Swap / Kesintisiz Gecis)
  • Otomatik Olceklendirme (Auto-Scaling): GM1, GM2, GM3...
  • SQLite Zero-Config Veritabani (Sifre yok, tam otomatik!)
  • Canli Web Paneli ve Konsol Eklendi (Render 502 Fix)
"""

import asyncio, json, os, pathlib, struct, sys
import threading, zlib, time, http.server, urllib.request, urllib.parse
import subprocess, glob, sqlite3
from collections import deque
import datetime

try:
    import aiosqlite
except ImportError:
    print("[SISTEM] 'aiosqlite' bulunamadi! 'pip install aiosqlite' komutunu calistirin.")
    sys.exit(1)

# ══════════════════════════════════════════════════════════
#  CANLI LOG TAMPONU (SSE KONSOL İÇİN)
# ══════════════════════════════════════════════════════════

_LOG_BUF     = deque(maxlen=300) 
_LOG_LOCK    = threading.Lock()
_SSE_CLIENTS = []
_SSE_LOCK    = threading.Lock()

_LOG_COLORS = {
    "[CONN]":   "#4ecca3", "[JOIN]":   "#4ecca3",
    "[QUIT]":   "#f8b400", "[BORE]":   "#7ec8e3",
    "[REG]":    "#a8edea", "[UNREG]":  "#f8b400",
    "[PROXY]":  "#4ecca3", "[HTTP]":   "#555",
    "[MC]":     "#c5a3ff", "[DB]":     "#f8b400",
    "[API]":    "#555",    "[ERR]":    "#ff6b6b",
}

def _log_color(line):
    for tag, color in _LOG_COLORS.items():
        if tag in line: return color
    return "#c8c8c8"

class _TeeLogger:
    def __init__(self, orig): self._orig = orig
    def write(self, text):
        self._orig.write(text)
        stripped = text.strip()
        if stripped:
            ts    = datetime.datetime.now().strftime("%H:%M:%S")
            entry = {"ts": ts, "msg": stripped, "color": _log_color(stripped)}
            with _LOG_LOCK: _LOG_BUF.append(entry)
            payload = f"data: {json.dumps(entry)}\n\n"
            with _SSE_LOCK:
                dead = []
                for q in _SSE_CLIENTS:
                    try:    q.put_nowait(payload)
                    except: dead.append(q)
                for q in dead: _SSE_CLIENTS.remove(q)
    def flush(self):  self._orig.flush()
    def isatty(self): return False

sys.stdout = _TeeLogger(sys.stdout)
sys.stderr = _TeeLogger(sys.stderr)

# ══════════════════════════════════════════════════════════
#  SİSTEM DEĞİŞKENLERİ VE OTOMATİK VERİTABANI
# ══════════════════════════════════════════════════════════

MODE          = os.environ.get("ENGINE_MODE", "gameserver")
if "wc-yccy" in os.environ.get("RENDER_EXTERNAL_HOSTNAME", ""):
    MODE = "all"

HTTP_PORT     = int(os.environ.get("PORT", 8080))
MC_PORT       = int(os.environ.get("MC_PORT", 25565))
CUBERITE_PORT = 25566 if MODE == "all" else MC_PORT

DATA_DIR      = os.environ.get("DATA_DIR", "/data")
SERVER_DIR    = os.environ.get("SERVER_DIR", "/server")
BORE_FILE     = "/tmp/bore_address.txt"
DB_FILE       = f"{DATA_DIR}/hub.db"

_current_bore_addr = None
_active_players = []

async def init_db():
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    label TEXT PRIMARY KEY,
                    host TEXT,
                    port INTEGER,
                    players INTEGER DEFAULT 0,
                    last_seen INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    username TEXT PRIMARY KEY,
                    uuid TEXT,
                    last_server TEXT,
                    inventory TEXT,
                    pos_x REAL DEFAULT 0,
                    pos_y REAL DEFAULT 5,
                    pos_z REAL DEFAULT 0,
                    health REAL DEFAULT 20
                )
            """)
            await db.commit()
        print(f"[DB] SQLite Veritabani Otomatik Olarak Hazirlandi: {DB_FILE}")
    except Exception as e:
        print(f"[DB] KRITIK HATA: Veritabani Olusturulamadi -> {e}")

# ══════════════════════════════════════════════════════════
#  CUBERITE AYARLARI VE LUA EKLENTİSİ (TELEPORT YÜZÜĞÜ)
# ══════════════════════════════════════════════════════════

SETTINGS_INI = f"""
[Authentication]
Authenticate=0
OnlineMode=0
ServerID=WCHubEngine

[Plugins]
Plugin=WCHub

[Server]
Description=Minecraft Distributed Hub
MaxPlayers=100
Port={CUBERITE_PORT}
Ports={CUBERITE_PORT}
NetworkCompressionThreshold=-1
"""

WORLD_INI = """
[General]
Gamemode=0
WorldType=FLAT
AllowFlight=1
[SpawnPosition]
MaxViewDistance=4 
X=0
Y=5
Z=0
"""

PLUGIN_MAIN = """
local ProxyURL = "http://127.0.0.1:8080"
if os.getenv("PROXY_URL") then ProxyURL = os.getenv("PROXY_URL") end

local function Split(str, sep)
    local res = {}
    for w in string.gmatch(str, "([^"..sep.."]+)") do table.insert(res, w) end
    return res
end

function Initialize(Plugin)
    Plugin:SetName("WCHub")
    Plugin:SetVersion(3)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_JOINED, OnPlayerJoined)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_DESTROYED, OnPlayerDestroyed)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_RIGHT_CLICK, OnRightClick)
    
    cRoot:Get():GetDefaultWorld():ScheduleTask(200, PeriodicSave)
    LOG("[HUB] WCHub aktif! Yuzuk sistemi devrede.")
    return true
end

function GiveRing(Player)
    local inv = Player:GetInventory()
    local hasRing = false
    for i=0, 35 do
        local item = inv:GetSlot(i)
        if item.m_ItemType == E_ITEM_COMPASS then hasRing = true break end
    end
    if not hasRing then
        local ring = cItem(E_ITEM_COMPASS, 1)
        ring.m_CustomName = "§eSunucu Secici §7(Sag Tik)"
        inv:AddItem(ring)
    end
end

function OnPlayerJoined(Player)
    GiveRing(Player)
    Player:SendMessageSuccess("Merkezi Hub'a Hos Geldin!")
end

function OnPlayerDestroyed(Player)
    local name = Player:GetName()
    local x, y, z = Player:GetPosX(), Player:GetPosY(), Player:GetPosZ()
    local hp = Player:GetHealth()
    local payload = string.format('{"x":%f,"y":%f,"z":%f,"hp":%f}', x, y, z, hp)
    
    local req = cNetwork::CreateRequest(ProxyURL .. "/api/player?name=" .. name)
    req:SetMethod("POST")
    cNetwork:PostData(req, payload, function() end)
end

function PeriodicSave(World)
    World:ForEachPlayer(function(Player)
        local name = Player:GetName()
        local x, y, z = Player:GetPosX(), Player:GetPosY(), Player:GetPosZ()
        local hp = Player:GetHealth()
        local payload = string.format('{"x":%f,"y":%f,"z":%f,"hp":%f}', x, y, z, hp)
        
        local req = cNetwork::CreateRequest(ProxyURL .. "/api/player?name=" .. name)
        req:SetMethod("POST")
        cNetwork:PostData(req, payload, function() end)
    end)
    World:ScheduleTask(200, PeriodicSave)
end

function OnRightClick(Player, BlockX, BlockY, BlockZ, BlockFace, CursorX, CursorY, CursorZ)
    local EquippedItem = Player:GetEquippedItem()
    if EquippedItem.m_ItemType == E_ITEM_COMPASS then
        cNetwork:Get(ProxyURL .. "/api/servers", function(Body, Data)
            if Body and Body ~= "" then
                Player:SendMessageInfo("Aktif Sunucular Listeleniyor...")
                local servers = Split(Body, ";")
                for i, srv in ipairs(servers) do
                    local parts = Split(srv, ":")
                    if #parts == 2 then
                        local label = parts[1]
                        local count = parts[2]
                        Player:SendMessage(cCompositeChat():AddTextPart("§8[§b" .. label .. "§8] §7- Aktif: §e" .. count .. " oyuncu ")
                            :AddRunCommandPart("§a[BAGLAN]", "/wc_transfer " .. label))
                    end
                end
            else
                Player:SendMessageFailure("Sunuculara ulasilamadi.")
            end
        end)
    end
    return false
end
"""

def write_configs(server_dir=SERVER_DIR):
    files = {
        f"{server_dir}/settings.ini": SETTINGS_INI.strip(),
        f"{server_dir}/world/world.ini": WORLD_INI.strip(),
        f"{server_dir}/Plugins/WCHub/Info.lua": 'g_PluginInfo = {Name="WCHub", Version="3"}',
        f"{server_dir}/Plugins/WCHub/main.lua": PLUGIN_MAIN.strip(),
    }
    for path, content in files.items():
        try:
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(path).write_text(content + "\n", encoding="utf-8")
        except Exception: pass

# ══════════════════════════════════════════════════════════
#  PROTOKOL & PAKET İŞLEME
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

async def vi_rd(reader):
    r = shift = 0
    while True:
        b = (await reader.readexactly(1))[0]
        r |= (b & 0x7F) << shift
        if not (b & 0x80): return r
        shift += 7

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
    if comp < 0: return vi_enc(len(data)) + data
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

# ══════════════════════════════════════════════════════════
#  PROXY: YÜK DENGELEME VE BUNGEECORD HOT-SWAP
# ══════════════════════════════════════════════════════════

class PlayerConn:
    def __init__(self, cr, cw):
        self.client_r = cr
        self.client_w = cw
        self.server_r = None
        self.server_w = None
        self.comp = -1
        self.username = "?"
        self.current_label = ""
        self.play_state = False

    async def get_target_server(self, requested_label=None):
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            if requested_label:
                async with db.execute("SELECT * FROM servers WHERE label=?", (requested_label,)) as cur:
                    return await cur.fetchone()
            
            async with db.execute("SELECT last_server FROM players WHERE username=?", (self.username,)) as cur:
                p_row = await cur.fetchone()
                if p_row and p_row['last_server']:
                    async with db.execute("SELECT * FROM servers WHERE label=?", (p_row['last_server'],)) as scur:
                        s_row = await scur.fetchone()
                        if s_row and (int(time.time()) - s_row['last_seen']) < 60:
                            return s_row
            
            async with db.execute("SELECT * FROM servers WHERE players < 100 AND (CAST(strftime('%s', 'now') AS INTEGER) - last_seen) < 60 ORDER BY players ASC LIMIT 1") as cur:
                return await cur.fetchone()

    async def connect_backend(self, host, port):
        if self.server_w: self.server_w.close()
        self.server_r, self.server_w = await asyncio.open_connection(host, port, limit=2**20)

    async def hot_swap(self, target_label):
        if self.current_label == target_label: return
        srv = await self.get_target_server(target_label)
        
        if not srv:
            msg = json.dumps({"text": f"{target_label} bulunamadi veya kapali!", "color": "red"})
            self.client_w.write(pkt_make(0x02, mc_str_enc(msg) + bytes([0]), self.comp))
            return

        self.play_state = False
        await self.connect_backend(srv['host'], srv['port'])
        
        hs = vi_enc(47) + mc_str_enc(srv['host']) + struct.pack(">H", srv['port']) + vi_enc(2)
        self.server_w.write(pkt_make(0x00, hs, -1))
        self.server_w.write(pkt_make(0x00, mc_str_enc(self.username), -1))
        await self.server_w.drain()
        
        while True:
            pid, payload, raw = await pkt_read(self.server_r, self.comp)
            if pid == 0x01:
                dim = payload[4]
                respawn_fake = struct.pack(">i", -1 if dim == 0 else 0) + payload[5:8] + mc_str_enc("default")
                respawn_real = struct.pack(">i", dim) + payload[5:8] + mc_str_enc("default")
                
                self.client_w.write(pkt_make(0x07, respawn_fake, self.comp))
                self.client_w.write(pkt_make(0x07, respawn_real, self.comp))
                
                pos = struct.pack(">dddff", 0.0, 5.0, 0.0, 0.0, 0.0) + bytes([0])
                self.client_w.write(pkt_make(0x08, pos, self.comp))
                await self.client_w.drain()
                
                self.current_label = target_label
                self.play_state = True
                
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("UPDATE players SET last_server=? WHERE username=?", (target_label, self.username))
                    await db.commit()
                break
            
    async def pipe_c2s(self):
        try:
            while True:
                pid, payload, raw = await pkt_read(self.client_r, self.comp)
                
                if pid == 0x01 and self.play_state: 
                    msg, _ = mc_str_dec(payload)
                    if msg.startswith("/wc_transfer "):
                        target = msg.split(" ")[1]
                        asyncio.ensure_future(self.hot_swap(target))
                        continue
                    elif not msg.startswith("/"):
                        formatted = json.dumps({"text": f"§8[§b{self.current_label}§8] §7{self.username}§f: {msg}"})
                        b_pkt = pkt_make(0x02, mc_str_enc(formatted) + bytes([0]), self.comp)
                        for c in list(_active_players):
                            if c.play_state:
                                try: c.client_w.write(b_pkt)
                                except: pass
                        continue 

                if self.server_w:
                    self.server_w.write(raw)
                    await self.server_w.drain()
        except: pass

    async def pipe_s2c(self):
        try:
            while True:
                if not self.server_r: await asyncio.sleep(0.1); continue
                pid, payload, raw = await pkt_read(self.server_r, self.comp)
                if pid == 0x03 and self.comp < 0:
                    self.comp, _ = vi_dec(payload)
                self.client_w.write(raw)
                await self.client_w.drain()
        except: pass

    async def run(self):
        try:
            pid, payload, raw = await pkt_read(self.client_r, -1)
            p=0; _,p=vi_dec(payload,p); _,p=mc_str_dec(payload,p); p+=2; next_state,_=vi_dec(payload,p)
            
            if next_state == 1:
                status_json = json.dumps({
                    "version": {"name": "1.8.x", "protocol": 47},
                    "players": {"max": 1000, "online": len(_active_players), "sample": []},
                    "description": {"text": f"§bWC Hub Agi §8- §e{len(_active_players)} Oyuncu Aktif"}
                })
                self.client_w.write(pkt_make(0x00, mc_str_enc(status_json), -1))
                await self.client_w.drain()
                return

            if next_state == 2:
                pid2, payload2, raw2 = await pkt_read(self.client_r, -1)
                self.username, _ = mc_str_dec(payload2)
                
                srv = await self.get_target_server()
                if not srv:
                    self.client_w.write(pkt_make(0x00, mc_str_enc(json.dumps({"text":"§cSunucu bulunamadi veya hepsi kapali."})), -1))
                    return

                self.current_label = srv['label']
                await self.connect_backend(srv['host'], srv['port'])
                self.server_w.write(raw)
                self.server_w.write(raw2)
                await self.server_w.drain()
                
                _active_players.append(self)
                self.play_state = True
                print(f"[JOIN] {self.username} -> {self.current_label} sunucusuna girdi.")
                
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("INSERT OR IGNORE INTO players (username, last_server) VALUES (?, ?)", (self.username, self.current_label))
                    await db.commit()

                await asyncio.gather(self.pipe_s2c(), self.pipe_c2s())
        except Exception: pass
        finally:
            if self in _active_players: _active_players.remove(self)
            for w in (self.client_w, self.server_w):
                if w:
                    try: w.close()
                    except: pass

async def handle_player(cr, cw):
    await PlayerConn(cr, cw).run()

# ══════════════════════════════════════════════════════════
#  HTML WEB PANEL VE HTTP API
# ══════════════════════════════════════════════════════════

HTML = """\
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>WC Hub Engine</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root{{--bg:#0d0f1a;--panel:#111827;--border:#1e3a5f;--accent:#00ffc8;--accent2:#0099ff;--warn:#f8b400;--err:#ff4f4f;--dim:#4a5568;--text:#cbd5e0;}}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;min-height:100vh;padding:20px;
          background-image:radial-gradient(ellipse 80% 60% at 50% -10%,#0a2a4a55,transparent),
          repeating-linear-gradient(0deg,transparent,transparent 39px,#1e3a5f18 39px,#1e3a5f18 40px),
          repeating-linear-gradient(90deg,transparent,transparent 39px,#1e3a5f18 39px,#1e3a5f18 40px);}}
    .wrap{{max-width:920px;margin:0 auto;display:flex;flex-direction:column;gap:14px}}
    .header{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);padding-bottom:12px}}
    .logo{{font-family:'Rajdhani',sans-serif;font-size:1.7rem;font-weight:700;color:var(--accent);letter-spacing:.08em;text-shadow:0 0 20px #00ffc855}}
    .logo span{{color:var(--text);font-weight:400}}
    .live-dot{{width:8px;height:8px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:6px;box-shadow:0 0 6px var(--accent);animation:blink 1.4s ease-in-out infinite}}
    @keyframes blink{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.4;transform:scale(.7)}}}}
    .addr-box{{background:linear-gradient(135deg,#0a2a4a,#0a1a35);border:1px solid var(--accent);border-radius:8px;padding:12px 18px;display:flex;align-items:center;gap:14px;box-shadow:0 0 24px #00ffc81a}}
    .addr-lbl{{font-size:.68rem;color:var(--dim);white-space:nowrap}}
    .addr-val{{font-size:1.25rem;color:var(--accent);flex:1;word-break:break-all;text-shadow:0 0 12px #00ffc844}}
    .copy-btn{{background:#00ffc815;border:1px solid var(--accent);color:var(--accent);border-radius:6px;padding:6px 14px;font-size:.73rem;cursor:pointer;font-family:'Share Tech Mono',monospace;transition:all .15s;white-space:nowrap}}
    .copy-btn:hover{{background:var(--accent);color:#000}}
    .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
    .stat{{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;position:relative;overflow:hidden}}
    .stat::before{{content:'';position:absolute;inset:0;background:linear-gradient(135deg,#00ffc808,transparent);pointer-events:none}}
    .stat-val{{font-size:2rem;color:var(--accent);font-family:'Rajdhani',sans-serif;font-weight:700;line-height:1}}
    .stat-lbl{{font-size:.68rem;color:var(--dim);margin-top:4px}}
    .panel{{background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
    .panel-hdr{{display:flex;align-items:center;justify-content:space-between;padding:9px 16px;border-bottom:1px solid var(--border);background:#0a1428}}
    .panel-hdr-title{{font-size:.72rem;color:var(--accent);letter-spacing:.1em;text-transform:uppercase}}
    table{{width:100%;border-collapse:collapse}}
    th{{color:var(--dim);font-size:.68rem;padding:7px 16px;text-align:left;text-transform:uppercase;letter-spacing:.08em;background:#0a1428;border-bottom:1px solid var(--border)}}
    td{{padding:8px 16px;font-size:.82rem;border-bottom:1px solid #1e3a5f33}}
    tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:#1e3a5f22}}
    .sdot{{width:7px;height:7px;border-radius:50%;background:var(--accent);display:inline-block;margin-right:6px;box-shadow:0 0 5px var(--accent);animation:blink 1.4s infinite}}
    .son{{color:var(--accent)}}
    .con-toolbar{{display:flex;align-items:center;justify-content:space-between;padding:7px 14px;background:#070d1a;border-bottom:1px solid var(--border)}}
    .win-btns{{display:flex;gap:6px}}
    .wb{{width:10px;height:10px;border-radius:50%}}
    .wb-r{{background:#ff5f56}}.wb-y{{background:#ffbd2e}}.wb-g{{background:#27c93f}}
    .con-title{{font-size:.68rem;color:var(--dim);letter-spacing:.1em;margin-left:8px}}
    .con-right{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
    .flt{{background:transparent;border:1px solid var(--border);color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:.63rem;border-radius:4px;padding:3px 8px;cursor:pointer;transition:all .15s}}
    .flt.on{{border-color:var(--accent);color:var(--accent)}}
    .flt:hover{{border-color:var(--accent2);color:var(--accent2)}}
    .clr{{background:transparent;border:1px solid var(--border);color:var(--dim);font-family:'Share Tech Mono',monospace;font-size:.63rem;border-radius:4px;padding:3px 8px;cursor:pointer;transition:all .15s}}
    .clr:hover{{border-color:var(--err);color:var(--err)}}
    .sse-ind{{display:flex;align-items:center;gap:4px;font-size:.62rem;color:var(--dim)}}
    .sse-d{{width:6px;height:6px;border-radius:50%;background:var(--dim)}}
    .sse-d.live{{background:var(--accent);box-shadow:0 0 4px var(--accent)}}
    #con{{background:#070d1a;height:340px;overflow-y:auto;padding:10px 14px;font-size:.76rem;line-height:1.75;scroll-behavior:smooth}}
    #con::-webkit-scrollbar{{width:4px}}
    #con::-webkit-scrollbar-track{{background:#0a0f1e}}
    #con::-webkit-scrollbar-thumb{{background:#1e3a5f;border-radius:3px}}
    .ll{{display:flex;gap:8px}}
    .lt{{color:var(--dim);flex-shrink:0;font-size:.66rem;padding-top:1px}}
    .lm{{word-break:break-all;flex:1}}
  </style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">⛏ WC<span>-HUB</span></div>
    <div style="font-size:.72rem;color:var(--dim)">
      <span class="live-dot"></span>{mode_label} &bull; <span id="hdr-p">{player_count}</span> oyuncu aktif
    </div>
  </div>
  {addr_block}
  <div class="stats">
    <div class="stat"><div class="stat-val" id="s-p">{player_count}</div><div class="stat-lbl">Aktif Oyuncu</div></div>
    <div class="stat"><div class="stat-val" id="s-b">{registered_players}</div><div class="stat-lbl">Kayıtlı Hesap</div></div>
    <div class="stat"><div class="stat-val" id="s-s">{server_count}</div><div class="stat-lbl">Game Server</div></div>
  </div>
  <div class="panel">
    <div class="panel-hdr"><span class="panel-hdr-title">Game Servers (Hub)</span></div>
    <table id="srv-tbl"><tr><th>Sunucu</th><th>Oyuncu</th><th>Durum</th></tr>{rows}</table>
  </div>
  <div class="panel">
    <div class="con-toolbar">
      <div style="display:flex;align-items:center">
        <div class="win-btns"><div class="wb wb-r"></div><div class="wb wb-y"></div><div class="wb wb-g"></div></div>
        <span class="con-title">CANLI KONSOL</span>
      </div>
      <div class="con-right">
        <button class="flt on" data-f="">TÜMÜ</button>
        <button class="flt" data-f="DB,API">VERİTABANI</button>
        <button class="flt" data-f="JOIN,QUIT,PROXY">BUNGEE</button>
        <button class="clr" id="clrBtn">TEMİZLE</button>
        <div class="sse-ind"><div class="sse-d" id="sseDot"></div><span id="sseLbl">bağlanıyor</span></div>
      </div>
    </div>
    <div id="con"><div style="color:#2a4a6a;font-size:.73rem;padding:4px">— konsol yükleniyor —</div></div>
  </div>
</div>
<script>
(function(){{
  const con=document.getElementById('con'),dot=document.getElementById('sseDot'),lbl=document.getElementById('sseLbl');
  let autoScroll=true,activeFilter='',allLines=[];
  con.addEventListener('scroll',()=>{{autoScroll=con.scrollTop+con.clientHeight>=con.scrollHeight-30;}});
  function esc(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
  function renderLine(e){{
    const d=document.createElement('div');d.className='ll';d.dataset.msg=e.msg;
    d.innerHTML='<span class="lt">'+e.ts+'</span><span class="lm" style="color:'+e.color+'">'+esc(e.msg)+'</span>';
    return d;
  }}
  function applyFilter(){{
    const f=activeFilter;
    con.querySelectorAll('.ll').forEach(el=>{{const m=el.dataset.msg||'';el.style.display=(!f||f.split(',').some(k=>m.includes('['+k+']')))?'':'none';}});
  }}
  document.querySelectorAll('.flt').forEach(btn=>{{
    btn.addEventListener('click',()=>{{document.querySelectorAll('.flt').forEach(b=>b.classList.remove('on'));btn.classList.add('on');activeFilter=btn.dataset.f;applyFilter();}});
  }});
  document.getElementById('clrBtn').addEventListener('click',()=>{{con.innerHTML='';allLines=[];}});
  function addLine(e){{
    allLines.push(e);
    if(allLines.length>300){{allLines.shift();const f=con.querySelector('.ll');if(f)f.remove();}}
    const el=renderLine(e);
    const f=activeFilter;
    if(f&&!f.split(',').some(k=>e.msg.includes('['+k+']')))el.style.display='none';
    con.appendChild(el);if(autoScroll)con.scrollTop=con.scrollHeight;
  }}
  fetch('/api/logs/history').then(r=>r.json()).then(arr=>{{con.innerHTML='';arr.forEach(e=>addLine(e));}}).catch(()=>{{}});
  function connectSSE(){{
    dot.className='sse-d';lbl.textContent='bağlanıyor...';
    const es=new EventSource('/api/logs/stream');
    es.onopen=()=>{{dot.className='sse-d live';lbl.textContent='canlı';}};
    es.onmessage=e=>{{try{{addLine(JSON.parse(e.data));}}catch(x){{}}}};
    es.onerror=()=>{{dot.className='sse-d';lbl.textContent='yeniden bağlanıyor...';es.close();setTimeout(connectSSE,3000);}};
  }}
  connectSSE();
  function refreshStats(){{
    fetch('/api/status').then(r=>r.json()).then(d=>{{
      document.getElementById('s-p').textContent=d.players;
      document.getElementById('s-b').textContent=d.registered;
      document.getElementById('s-s').textContent=d.servers;
      document.getElementById('hdr-p').textContent=d.players;
      const t=document.getElementById('srv-tbl');
      if(t)t.innerHTML='<tr><th>Sunucu</th><th>Oyuncu</th><th>Durum</th></tr>'+d.table_rows;
      if(d.addr){{const av=document.querySelector('.addr-val');if(av)av.textContent=d.addr;}}
    }}).catch(()=>{{}});
  }}
  setInterval(refreshStats,5000);
}})();
</script>
</body></html>"""

def _build_rows():
    rows = ""
    server_count = 0
    registered = 0
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        cur.execute("SELECT label, players, last_seen FROM servers")
        db_servers = cur.fetchall()
        now = int(time.time())
        for s in db_servers:
            age_sec = now - s['last_seen']
            age_str = f"{age_sec//60}dk önce"
            status = '<span class="sdot"></span><span class="son">Aktif</span>' if age_sec < 60 else f'<span style="color:var(--warn)">Uykuda ({age_str})</span>'
            rows += f"<tr><td>{s['label']}</td><td>{s['players']} oyuncu</td><td>{status}</td></tr>"
            server_count += 1
            
        cur.execute("SELECT COUNT(*) FROM players")
        registered = cur.fetchone()[0]
        conn.close()
    except: pass
    
    if not rows:
        rows = '<tr><td colspan="3" style="text-align:center;color:var(--dim)">Sunucu bekleniyor...</td></tr>'
    return rows, server_count, registered

class HttpHandler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        if self.path == "/":
            rows, s_count, reg_count = _build_rows()
            bore = _current_bore_addr
            if bore:
                addr_block = (f'<div class="addr-box"><div><div class="addr-lbl">MİNECRAFT ADRESİ (Bu adresi kopyala)</div>'
                              f'<div class="addr-val">{bore}</div></div>'
                              f'<button class="copy-btn" onclick="navigator.clipboard.writeText(\'{bore}\');'
                              f'this.textContent=\'Kopyalandı\';setTimeout(()=>this.textContent=\'Kopyala\',1500)">Kopyala</button></div>')
            else:
                addr_block = ('<div class="addr-box" style="border-color:var(--warn)">'
                              '<div class="addr-val" style="color:var(--warn);font-size:.9rem">⏳ Tunnel başlatılıyor...</div></div>')
            
            body = HTML.format(addr_block=addr_block, player_count=len(_active_players), 
                               registered_players=reg_count, server_count=s_count, rows=rows, mode_label=MODE.upper()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/status":
            rows, s_count, reg_count = _build_rows()
            payload = {"players": len(_active_players), "registered": reg_count,
                       "servers": s_count, "mode": MODE.upper(), "addr": _current_bore_addr or "", "table_rows": rows}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/logs/stream":
            import queue as _q
            q = _q.Queue(maxsize=200)
            with _SSE_LOCK: _SSE_CLIENTS.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    try:
                        payload = q.get(timeout=25)
                        self.wfile.write(payload.encode()); self.wfile.flush()
                    except _q.Empty:
                        self.wfile.write(b": ping\n\n"); self.wfile.flush()
            except Exception: pass
            finally:
                with _SSE_LOCK:
                    if q in _SSE_CLIENTS: _SSE_CLIENTS.remove(q)
            return

        if self.path == "/api/logs/history":
            with _LOG_LOCK: data = list(_LOG_BUF)
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/servers":
            try:
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT label, players FROM servers WHERE (CAST(strftime('%s', 'now') AS INTEGER) - last_seen) < 60")
                rows = cur.fetchall()
                conn.close()
                resp = ";".join([f"{r['label']}:{r['players']}" for r in rows])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(resp.encode())
            except Exception:
                self.send_response(500)
                self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0: self.send_response(400); self.end_headers(); return
        data = self.rfile.read(length).decode('utf-8')

        if self.path.startswith("/api/player?name="):
            name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('name', [''])[0]
            try:
                p_data = json.loads(data)
                conn = sqlite3.connect(DB_FILE)
                conn.execute("UPDATE players SET pos_x=?, pos_y=?, pos_z=?, health=? WHERE username=?",
                            (p_data.get('x',0), p_data.get('y',5), p_data.get('z',0), p_data.get('hp',20), name))
                conn.commit()
                conn.close()
                self.send_response(200)
                self.end_headers()
            except:
                self.send_response(500)
                self.end_headers()

        elif self.path == "/api/register":
            try:
                s_data = json.loads(data)
                host, port = s_data['host'], s_data['port']
                
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                
                cur.execute("SELECT label FROM servers WHERE host=? AND port=?", (host, port))
                row = cur.fetchone()
                
                if row:
                    label = row[0]
                    conn.execute("UPDATE servers SET last_seen=CAST(strftime('%s', 'now') AS INTEGER) WHERE label=?", (label,))
                else:
                    cur.execute("SELECT COUNT(*) FROM servers")
                    count = cur.fetchone()[0]
                    label = f"GM{count+1}"
                    conn.execute("INSERT INTO servers (label, host, port, last_seen) VALUES (?, ?, ?, CAST(strftime('%s', 'now') AS INTEGER))", (label, host, port))
                
                conn.commit()
                conn.close()
                
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"label": label}).encode())
                print(f"[API] Sunucu Kaydedildi: {label} ({host}:{port})")
            except Exception as e:
                print(f"[API] Kayit Hatasi: {e}")
                self.send_response(500)
                self.end_headers()

    def log_message(self, format, *args): pass

# ══════════════════════════════════════════════════════════
#  BAŞLATICI YÖNTEMLER
# ══════════════════════════════════════════════════════════

def run_http():
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)
    srv.serve_forever()

def _strip_ansi(text):
    import re
    return re.sub(r'\x1b\[[0-9;]*[mK]|\x1b\[\d*[A-Za-z]|\x1b\(\w', '', text)

def run_bore(port=MC_PORT):
    global _current_bore_addr
    import re
    while True:
        try:
            proc = subprocess.Popen(["bore", "local", str(port), "--to", "bore.pub"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = _strip_ansi(line.rstrip())
                if not line: continue
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    _current_bore_addr = f"bore.pub:{m.group(1)}"
                    if MODE == "gameserver" or MODE == "all":
                        _register_with_proxy(_current_bore_addr)
            proc.wait()
        except Exception: pass
        time.sleep(10)

def _register_with_proxy(bore_addr):
    proxy_url = os.environ.get("PROXY_URL", f"http://127.0.0.1:{HTTP_PORT}")
    host, port_str = bore_addr.split(":")
    body = json.dumps({"host": host, "port": int(port_str)}).encode()
    try:
        req = urllib.request.Request(f"{proxy_url}/api/register", data=body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5)
        label = json.loads(resp.read().decode())['label']
        print(f"[REG] Basariyla kayit olundu! Bu sunucunun adi: {label}")
    except Exception as e:
        print(f"[REG] HATA: Proxy'e kayit olunamadi. -> {e}")

def run_cuberite():
    write_configs()
    mc_bin = next(iter(glob.glob("/server/**/Cuberite", recursive=True)), None)
    if not mc_bin: return
    mc_dir = str(pathlib.Path(mc_bin).parent)
    os.chmod(mc_bin, 0o755)
    
    while True:
        subprocess.Popen([mc_bin], cwd=mc_dir).wait()
        time.sleep(5)

async def run_proxy():
    await init_db()
    server = await asyncio.start_server(handle_player, "0.0.0.0", MC_PORT)
    print(f"[PROXY] SQLite Hub Mimarisi {MC_PORT} portunda dinliyor...")
    async with server:
        await server.serve_forever()

def main():
    print(f"""
+--------------------------------------------------+
|  Minecraft BungeeCord Hub Engine                 |
|  Mod: {MODE:<43}|
|  DB : SQLite (Tam Otomatik Zero-Config)          |
+--------------------------------------------------+""")

    if MODE == "proxy":
        threading.Thread(target=run_http, daemon=True).start()
        asyncio.run(run_proxy())
    elif MODE == "gameserver":
        threading.Thread(target=run_bore, args=(CUBERITE_PORT,), daemon=True).start()
        run_cuberite()
    elif MODE == "all":
        threading.Thread(target=run_http, daemon=True).start()
        threading.Thread(target=run_bore, args=(CUBERITE_PORT,), daemon=True).start()
        threading.Thread(target=run_cuberite, daemon=True).start()
        time.sleep(3)
        asyncio.run(run_proxy())

if __name__ == "__main__": main()
