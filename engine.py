#!/usr/bin/env python3
"""
⛏️  Minecraft Ultimate Bungee Network & Anti-Dupe Engine
═══════════════════════════════════════════════════════════
  • Tünel Çakışması ve DB Zaman Uyuşmazlığı (KICK Hatası) ÇÖZÜLDÜ!
  • WCSync: Merkezi Envanter Senkronizasyonu GERİ EKLENDİ (API Entegreli)
  • WCHub: Pusula ile Sunucular Arası Kesintisiz Geçiş
  • Race Condition Engellendi (DB Kilit Sistemi)
"""

import asyncio, json, os, pathlib, struct, sys
import threading, zlib, time, http.server, urllib.request, urllib.parse
import subprocess, glob, sqlite3
from collections import deque
import datetime

# ══════════════════════════════════════════════════════════
#  SİSTEM DEĞİŞKENLERİ VE YAPILANDIRMA
# ══════════════════════════════════════════════════════════

MODE          = os.environ.get("ENGINE_MODE", "gameserver")
if "wc-yccy" in os.environ.get("RENDER_EXTERNAL_HOSTNAME", ""):
    MODE = "all"

HTTP_PORT     = int(os.environ.get("PORT", 8080))
MC_PORT       = int(os.environ.get("MC_PORT", 25565))
CUBERITE_PORT = 25566 if MODE == "all" else MC_PORT

DATA_DIR      = os.environ.get("DATA_DIR", "/data")
SERVER_DIR    = os.environ.get("SERVER_DIR", "/server")
DB_FILE       = f"{DATA_DIR}/hub.db"

_proxy_bore_addr = None
_active_players  = []
_DB_LOCK         = threading.Lock() # Race condition önleyici kilit

# ══════════════════════════════════════════════════════════
#  VERİTABANI İŞLEMLERİ (YENİLENDİ: Saat Uyuşmazlığı Çözüldü)
# ══════════════════════════════════════════════════════════

async def init_db():
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    label TEXT PRIMARY KEY, host TEXT, port INTEGER,
                    players INTEGER DEFAULT 0, last_seen INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    username TEXT PRIMARY KEY, last_server TEXT
                )
            """)
            await db.commit()
        print(f"[DB] SQLite Merkez Veritabani Hazir.")
    except Exception as e:
        print(f"[DB] HATA: Veritabani Olusturulamadi -> {e}")

# ══════════════════════════════════════════════════════════
#  LUA EKLENTİLERİ (WCSync, WCHub, Yaver Desteği)
# ══════════════════════════════════════════════════════════

SETTINGS_INI = f"""
[Authentication]
Authenticate=0
OnlineMode=0
ServerID=WCHubEngine

[Plugins]
Plugin=WCHub
Plugin=WCSync
Plugin=yaver

[Server]
Description=Minecraft Distributed Hub
MaxPlayers=100
Port={CUBERITE_PORT}
Ports={CUBERITE_PORT}
NetworkCompressionThreshold=-1
"""

# WCSync: Oyuncu girip çıktığında Python tetikleyicisi için log atar
WCSYNC_MAIN = """
function Initialize(Plugin)
    Plugin:SetName("WCSync")
    Plugin:SetVersion(2)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_JOINED, OnPlayerJoined)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_DESTROYED, OnPlayerDestroyed)
    cPluginManager:BindConsoleCommand("wcreload", HandleConsoleReload, "Python tetikleyici")
    cRoot:Get():GetDefaultWorld():ScheduleTask(200, PeriodicSave)
    LOG("[SYNC] WCSync aktif! Anti-Dupe devrede.")
    return true
end

function PeriodicSave(World)
    World:ForEachPlayer(function(Player)
        Player:SaveToDisk()
        LOG("WCSYNC_SAVE:" .. Player:GetName() .. ":" .. Player:GetUUID())
    end)
    World:ScheduleTask(200, PeriodicSave)
end

function OnPlayerJoined(Player) LOG("WCSYNC_JOIN:" .. Player:GetName() .. ":" .. Player:GetUUID()) end

function OnPlayerDestroyed(Player)
    Player:SaveToDisk()
    LOG("WCSYNC_QUIT:" .. Player:GetName() .. ":" .. Player:GetUUID())
end

function HandleConsoleReload(Split)
    if #Split > 1 then
        cRoot:Get():FindAndDoWithPlayer(Split[2], function(P)
            P:LoadFromDisk()
            P:SendMessageSuccess("Envanteriniz merkezden esitlendi!")
        end)
    end
    return true
end
"""

# WCHub: GUI menüsü ve Pusula
WCHUB_MAIN = """
local ProxyURL = "http://127.0.0.1:8080"
if os.getenv("PROXY_URL") then ProxyURL = os.getenv("PROXY_URL") end

local function Split(str, sep)
    local res = {}
    for w in string.gmatch(str, "([^"..sep.."]+)") do table.insert(res, w) end
    return res
end

function Initialize(Plugin)
    Plugin:SetName("WCHub")
    Plugin:SetVersion(2)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_JOINED, GiveRing)
    cPluginManager:AddHook(cPluginManager.HOOK_PLAYER_RIGHT_CLICK, OnRightClick)
    LOG("[HUB] WCHub aktif! Yuzuk sistemi devrede.")
    return true
end

function GiveRing(Player)
    local inv = Player:GetInventory()
    local hasRing = false
    for i=0, 35 do if inv:GetSlot(i).m_ItemType == E_ITEM_COMPASS then hasRing = true break end end
    if not hasRing then
        local ring = cItem(E_ITEM_COMPASS, 1)
        ring.m_CustomName = "§eSunucu Secici §7(Sag Tik)"
        inv:AddItem(ring)
    end
end

function OnRightClick(Player, BlockX, BlockY, BlockZ, BlockFace, CursorX, CursorY, CursorZ)
    local EquippedItem = Player:GetEquippedItem()
    if EquippedItem.m_ItemType == E_ITEM_COMPASS then
        cNetwork:Get(ProxyURL .. "/api/servers", function(Body, Data)
            if Body and Body ~= "" then
                Player:SendMessageInfo("§e--- Aktif Sunucular ---")
                local servers = Split(Body, ";")
                for i, srv in ipairs(servers) do
                    local parts = Split(srv, ":")
                    if #parts == 2 then
                        Player:SendMessage(cCompositeChat():AddTextPart("§8[§b" .. parts[1] .. "§8] §7- Aktif ")
                            :AddRunCommandPart("§a[GEÇİŞ YAP]", "/wc_transfer " .. parts[1]))
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
        f"{server_dir}/Plugins/WCSync/Info.lua": 'g_PluginInfo = {Name="WCSync", Version="2"}',
        f"{server_dir}/Plugins/WCSync/main.lua": WCSYNC_MAIN.strip(),
        f"{server_dir}/Plugins/WCHub/Info.lua": 'g_PluginInfo = {Name="WCHub", Version="2"}',
        f"{server_dir}/Plugins/WCHub/main.lua": WCHUB_MAIN.strip(),
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
#  PROXY: YÖNLENDİRİCİ VE ENVANTER KORUMALI HOT-SWAP
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
        self.is_swapping = False

    async def get_target_server(self, requested_label=None):
        import aiosqlite
        now = int(time.time())
        try:
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
                            if s_row and (now - s_row['last_seen']) < 60: return s_row
                
                async with db.execute("SELECT * FROM servers WHERE players < 100 AND (? - last_seen) < 60 ORDER BY players ASC LIMIT 1", (now,)) as cur:
                    return await cur.fetchone()
        except Exception as e: print(f"[PROXY] DB Hatasi: {e}"); return None

    async def hot_swap(self, target_label):
        if self.current_label == target_label: return
        self.is_swapping = True
        self.play_state = False
        
        # Oyuncuya bildirim gonder
        msg = json.dumps({"text": f"§a{target_label} sunucusuna geciliyor... Envanter senkronize ediliyor.", "color": "yellow"})
        self.client_w.write(pkt_make(0x02, mc_str_enc(msg) + bytes([0]), self.comp))
        await self.client_w.drain()

        # Eski sunucuyu kapat ki WCSYNC_QUIT calissin ve Json merkeze yollansin!
        if self.server_w:
            self.server_w.close()
            self.server_w = None
            self.server_r = None
            
        # Json merkeze islensin diye WCSYNC icin zorunlu bekleme suresi (Kritik Anti-Dupe Taktigi)
        await asyncio.sleep(2.5)
        
        srv = await self.get_target_server(target_label)
        if not srv:
            msg = json.dumps({"text": f"§c{target_label} baglantisi basarisiz!", "color": "red"})
            self.client_w.write(pkt_make(0x02, mc_str_enc(msg) + bytes([0]), self.comp))
            self.is_swapping = False
            return

        self.server_r, self.server_w = await asyncio.open_connection(srv['host'], srv['port'], limit=2**20)
        
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
                self.is_swapping = False
                
                import aiosqlite
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("UPDATE players SET last_server=? WHERE username=?", (target_label, self.username))
                    await db.commit()
                break
            
    async def pipe_c2s(self):
        while True:
            if self.is_swapping or not self.server_w:
                await asyncio.sleep(0.1); continue
            try:
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
                self.server_w.write(raw)
                await self.server_w.drain()
            except Exception:
                if not self.is_swapping: break

    async def pipe_s2c(self):
        while True:
            if self.is_swapping or not self.server_r:
                await asyncio.sleep(0.1); continue
            try:
                pid, payload, raw = await pkt_read(self.server_r, self.comp)
                if pid == 0x03 and self.comp < 0:
                    self.comp, _ = vi_dec(payload)
                self.client_w.write(raw)
                await self.client_w.drain()
            except Exception:
                if not self.is_swapping: break

    async def run(self):
        try:
            pid, payload, raw = await pkt_read(self.client_r, -1)
            p=0; _,p=vi_dec(payload,p); _,p=mc_str_dec(payload,p); p+=2; next_state,_=vi_dec(payload,p)
            
            if next_state == 1:
                status_json = json.dumps({
                    "version": {"name": "1.8.x", "protocol": 47},
                    "players": {"max": 1000, "online": len(_active_players), "sample": []},
                    "description": {"text": f"§bWC Merkezi Hub §8- §e{len(_active_players)} Aktif"}
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
                self.server_r, self.server_w = await asyncio.open_connection(srv['host'], srv['port'], limit=2**20)
                self.server_w.write(raw)
                self.server_w.write(raw2)
                await self.server_w.drain()
                
                _active_players.append(self)
                self.play_state = True
                print(f"[JOIN] {self.username} -> {self.current_label} sunucusuna girdi.")
                
                import aiosqlite
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
#  HTTP API VE DOSYA SENKRONİZASYONU
# ══════════════════════════════════════════════════════════

class HttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/player_file?name="):
            name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('name', [''])[0]
            name = "".join(c for c in name if c.isalnum() or c in "-_")
            filepath = f"{DATA_DIR}/players/{name}.json"
            if os.path.exists(filepath):
                body = pathlib.Path(filepath).read_bytes()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
            return

        if self.path == "/api/servers":
            try:
                conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; cur = conn.cursor()
                cur.execute("SELECT label, players FROM servers WHERE (? - last_seen) < 60 ORDER BY label ASC", (int(time.time()),))
                resp = ";".join([f"{r['label']}:{r['players']}" for r in cur.fetchall()])
                conn.close()
                self.send_response(200); self.end_headers(); self.wfile.write(resp.encode())
            except Exception: self.send_response(500); self.end_headers()

    def do_POST(self):
        if self.path.startswith("/api/player_file?name="):
            name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('name', [''])[0]
            name = "".join(c for c in name if c.isalnum() or c in "-_")
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            pathlib.Path(f"{DATA_DIR}/players").mkdir(parents=True, exist_ok=True)
            pathlib.Path(f"{DATA_DIR}/players/{name}.json").write_bytes(data)
            self.send_response(200); self.end_headers()
            return

        elif self.path == "/api/register":
            length = int(self.headers.get("Content-Length", 0))
            try:
                s_data = json.loads(self.rfile.read(length))
                host, port = s_data['host'], s_data['port']
                now = int(time.time())
                
                with _DB_LOCK: # Race Condition Kilidi!
                    conn = sqlite3.connect(DB_FILE)
                    cur = conn.cursor()
                    cur.execute("SELECT label FROM servers WHERE host=? AND port=?", (host, port))
                    row = cur.fetchone()
                    if row:
                        label = row[0]
                        conn.execute("UPDATE servers SET last_seen=? WHERE label=?", (now, label))
                    else:
                        cur.execute("SELECT COUNT(*) FROM servers")
                        label = f"GM{cur.fetchone()[0] + 1}"
                        conn.execute("INSERT INTO servers (label, host, port, last_seen) VALUES (?, ?, ?, ?)", (label, host, port, now))
                    conn.commit(); conn.close()
                
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({"label": label}).encode())
                print(f"[REG] Sunucu Islendi: {label} ({host}:{port})")
            except Exception as e:
                print(f"[REG] Kayit Hatasi: {e}"); self.send_response(500); self.end_headers()

    def log_message(self, format, *args): pass

# ══════════════════════════════════════════════════════════
#  BAŞLATICI YÖNTEMLER, CUBERITE VE TÜNEL
# ══════════════════════════════════════════════════════════

def run_http():
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)
    srv.serve_forever()

def run_bore_for_proxy():
    global _proxy_bore_addr
    import re
    while True:
        try:
            proc = subprocess.Popen(["bore", "local", str(MC_PORT), "--to", "bore.pub"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = re.sub(r'\x1b\[[0-9;]*[mK]|\x1b\[\d*[A-Za-z]|\x1b\(\w', '', line.rstrip())
                if not line: continue
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    _proxy_bore_addr = f"bore.pub:{m.group(1)}"
                    print(f"[BORE] Ana Yönlendirici (Proxy) Tüneli Açıldı! Adres: {_proxy_bore_addr}")
            proc.wait()
        except: pass
        time.sleep(5)

def run_bore_for_gameserver():
    import re
    proxy_url = os.environ.get("PROXY_URL", "")
    if not proxy_url: return
    current_gs_bore = None
    
    def heartbeat():
        while True:
            time.sleep(15)
            if current_gs_bore:
                try:
                    host, port_str = current_gs_bore.split(":")
                    req = urllib.request.Request(f"{proxy_url}/api/register", data=json.dumps({"host": host, "port": int(port_str)}).encode(), headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=5)
                except Exception: pass

    threading.Thread(target=heartbeat, daemon=True).start()

    while True:
        try:
            proc = subprocess.Popen(["bore", "local", str(CUBERITE_PORT), "--to", "bore.pub"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = re.sub(r'\x1b\[[0-9;]*[mK]|\x1b\[\d*[A-Za-z]|\x1b\(\w', '', line.rstrip())
                if not line: continue
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    current_gs_bore = f"bore.pub:{m.group(1)}"
                    print(f"[BORE] Alt Sunucu Tüneli: {current_gs_bore}")
            proc.wait()
        except: pass
        time.sleep(5)

def run_cuberite():
    write_configs()
    mc_bin = next(iter(glob.glob("/server/**/Cuberite", recursive=True)), None)
    if not mc_bin: return
    os.chmod(mc_bin, 0o755)
    
    persistent_world = f"{DATA_DIR}/world" if MODE == "all" else "/server/world"
    proxy_url = os.environ.get("PROXY_URL", f"http://127.0.0.1:{HTTP_PORT}")
    
    def _pipe_output(stream, proc):
        for raw in stream:
            line = raw.rstrip() if isinstance(raw, str) else raw.decode("utf-8", "replace").rstrip()
            if not line: continue
            
            # WCSync Tetikleyicileri (Envanter İndir/Yükle)
            if "WCSYNC_JOIN:" in line:
                def _do_join(ln):
                    try:
                        name, uuid = ln.split("WCSYNC_JOIN:")[1].strip().split(":")
                        uuid_clean = uuid.replace("-", "")
                        req = urllib.request.Request(f"{proxy_url}/api/player_file?name={name}")
                        data = urllib.request.urlopen(req, timeout=5).read()
                        
                        paths = [pathlib.Path(f"{persistent_world}/players/{uuid}.json"), pathlib.Path(f"{persistent_world}/players/{uuid_clean}.json")]
                        for p in paths:
                            p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
                        
                        time.sleep(1.0)
                        proc.stdin.write(f"wcreload {name}\n"); proc.stdin.flush()
                        print(f"[SYNC] {name} envanteri indirildi ve oyuna islendi.")
                    except Exception as e:
                        if "404" not in str(e): print(f"[SYNC] Hata: {e}")
                threading.Thread(target=_do_join, args=(line,), daemon=True).start()
                
            elif "WCSYNC_QUIT:" in line or "WCSYNC_SAVE:" in line:
                def _do_upload(ln):
                    try:
                        time.sleep(1.0)
                        tag = "WCSYNC_QUIT:" if "WCSYNC_QUIT:" in ln else "WCSYNC_SAVE:"
                        name, uuid = ln.split(tag)[1].strip().split(":")
                        uuid_clean = uuid.replace("-", "")
                        
                        p1 = pathlib.Path(f"{persistent_world}/players/{uuid}.json")
                        p2 = pathlib.Path(f"{persistent_world}/players/{uuid_clean}.json")
                        target_p = p1 if p1.exists() else (p2 if p2.exists() else None)
                        
                        if target_p:
                            req = urllib.request.Request(f"{proxy_url}/api/player_file?name={name}", data=target_p.read_bytes(), method="POST")
                            urllib.request.urlopen(req, timeout=5)
                            print(f"[SYNC] {name} envanteri merkeze yuklendi.")
                    except Exception as e: print(f"[SYNC] Upload Hatasi: {e}")
                threading.Thread(target=_do_upload, args=(line,), daemon=True).start()

    while True:
        proc = subprocess.Popen([mc_bin], cwd=str(pathlib.Path(mc_bin).parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=_pipe_output, args=(proc.stdout, proc), daemon=True).start()
        proc.wait(); time.sleep(5)

def register_local_cuberite():
    while True:
        time.sleep(15)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/api/register", data=json.dumps({"host": "127.0.0.1", "port": CUBERITE_PORT}).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception: pass

async def run_proxy():
    await init_db()
    server = await asyncio.start_server(handle_player, "0.0.0.0", MC_PORT)
    print(f"[PROXY] Hub Yönlendirici {MC_PORT} portunda hazir...")
    async with server: await server.serve_forever()

def main():
    print(f"""
+--------------------------------------------------+
|  Minecraft Bungee Network & Anti-Dupe Engine     |
|  Mod: {MODE:<43}|
+--------------------------------------------------+""")

    if MODE == "proxy":
        threading.Thread(target=run_http, daemon=True).start()
        threading.Thread(target=run_bore_for_proxy, daemon=True).start()
        asyncio.run(run_proxy())
        
    elif MODE == "gameserver":
        threading.Thread(target=run_bore_for_gameserver, daemon=True).start()
        run_cuberite()
        
    elif MODE == "all":
        threading.Thread(target=run_http, daemon=True).start()
        threading.Thread(target=run_bore_for_proxy, daemon=True).start()
        threading.Thread(target=run_cuberite, daemon=True).start()
        threading.Thread(target=register_local_cuberite, daemon=True).start()
        time.sleep(2)
        asyncio.run(run_proxy())

if __name__ == "__main__": main()
