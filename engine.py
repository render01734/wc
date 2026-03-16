#!/usr/bin/env python3
"""
⛏️  Minecraft Ultimate Bungee Network & Anti-Dupe Engine
═══════════════════════════════════════════════════════════
  • FIX: MySQL iptal edildi, Tam Otomatik SQLite (Tak-Çalıştır) geri döndü!
  • FIX: Kurt (/kurt) sistemi icin 'GetWolfType' Tarayicisi devrede.
  • WEB: Canli Konsol (Terminal) aktif.
  • MASTER: Tüm sunucuları tek noktadan dinleme ve komut/script senkronizasyonu aktif!
  • DİNAMİK: GitHub 'list' dosyası üzerinden sonsuz otomatik Lua eklenti desteği!
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

_proxy_bore_addr  = None
_active_players   = []
_DB_LOCK          = threading.Lock()
_cuberite_proc    = None
_STDIN_LOCK       = threading.Lock()   # Cuberite stdin erişimi için kilit

def _write_to_cuberite(cmd: str):
    """Cuberite stdin'ine thread-safe komut gönder."""
    with _STDIN_LOCK:
        if _cuberite_proc and _cuberite_proc.poll() is None:
            try:
                _cuberite_proc.stdin.write(cmd + "\n")
                _cuberite_proc.stdin.flush()
            except Exception:
                pass

# === MERKEZİ SENKRONİZASYON DEĞİŞKENLERİ ===
SYSTEM_LOGS = deque(maxlen=200)
_cmd_history = []
_cmd_counter = 0
_last_script_update = time.time()
_pending_remote_logs = []
_LOG_LOCK = threading.Lock()

def log_msg(text):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {text}"
    SYSTEM_LOGS.append(line)
    print(line)
    if MODE == "gameserver":
        with _LOG_LOCK:
            _pending_remote_logs.append(text)

# ══════════════════════════════════════════════════════════
#  VERİTABANI İŞLEMLERİ
# ══════════════════════════════════════════════════════════

async def init_db():
    pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    label TEXT PRIMARY KEY, host TEXT, port INTEGER,
                    players INTEGER DEFAULT 0, last_seen INTEGER,
                    restart_pending INTEGER DEFAULT 0
                )
            """)
            try: await db.execute("ALTER TABLE servers ADD COLUMN server_id TEXT")
            except: pass
            try: await db.execute("ALTER TABLE servers ADD COLUMN restart_pending INTEGER DEFAULT 0")
            except: pass
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    username TEXT PRIMARY KEY, last_server TEXT
                )
            """)
            await db.execute("DELETE FROM servers WHERE (? - last_seen) > 45", (int(time.time()),))
            await db.commit()
        log_msg("[DB] SQLite Merkez Veritabani Otomatik Kuruldu ve Hazir.")
    except Exception as e:
        log_msg(f"[DB] HATA: Veritabani Olusturulamadi -> {e}")

# ══════════════════════════════════════════════════════════
#  DİNAMİK GITHUB SCRIPT GÜNCELLEYİCİ VE YAPILANDIRMA
# ══════════════════════════════════════════════════════════

def update_and_configure(server_dir=SERVER_DIR):
    base_url = "https://raw.githubusercontent.com/Exma0/va/refs/heads/main"
    list_url = f"{base_url}/list"
    
    log_msg("[GÜNCELLEME] GitHub'dan dinamik eklenti listesi (list dosyası) kontrol ediliyor...")
    try:
        req = urllib.request.Request(list_url)
        lines = urllib.request.urlopen(req, timeout=10).read().decode('utf-8').splitlines()
    except Exception as e:
        log_msg(f"[GÜNCELLEME HATA] Liste çekilemedi: {e}")
        lines = ["wcsync.lua", "wchub.lua", "yaver.lua"] # Liste çekilemezse varsayılanlara dön
        
    plugin_names = []
    success_count = 0
    total_scripts = 0
    
    for line in lines:
        script_name = line.strip()
        if not script_name or not script_name.endswith('.lua'): continue
        total_scripts += 1
        
        # Klasör adı belirleme (eski eklentilerin büyük/küçük harf yapısını korumak için, yeniler otomatik açılır)
        if script_name == "wcsync.lua": folder_name = "WCSync"
        elif script_name == "wchub.lua": folder_name = "WCHub"
        else: folder_name = script_name[:-4] # Örn: yeni.lua -> klasör adı "yeni"
        
        plugin_names.append(folder_name)
        
        try:
            req = urllib.request.Request(f"{base_url}/{script_name}")
            code = urllib.request.urlopen(req, timeout=10).read().decode('utf-8')
            
            # DÜZELTME: Hem WCHub hem WCSync {PORT} placeholder kullanır.
            # Sadece WCHub için yapılıyordu; WCSync'te {PORT} literal metin
            # olarak kalıyor, ProxyURL hiç çözümlenemiyordu.
            if "{PORT}" in code:
                code = code.replace("{PORT}", str(HTTP_PORT))
                
            plugin_dir = f"{server_dir}/Plugins/{folder_name}"
            pathlib.Path(plugin_dir).mkdir(parents=True, exist_ok=True)
            
            # main.lua dosyasını yaz
            pathlib.Path(f"{plugin_dir}/main.lua").write_text(code + "\n", encoding="utf-8")
            
            # Cuberite'ın eklentiyi görmesi için zorunlu olan Info.lua dosyasını dinamik oluştur
            info_content = f'g_PluginInfo = {{Name="{folder_name}", Version="1"}}'
            pathlib.Path(f"{plugin_dir}/Info.lua").write_text(info_content + "\n", encoding="utf-8")
            
            success_count += 1
        except Exception as e:
            log_msg(f"[GÜNCELLEME HATA] {script_name} çekilemedi: {e}")
            
    # settings.ini dosyasını indirilen listeye göre dinamik olarak baştan yarat
    plugins_ini = "\n".join([f"Plugin={name}" for name in plugin_names])
    settings_ini = f"""
[Authentication]
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
        pathlib.Path(f"{server_dir}/settings.ini").write_text(settings_ini.strip() + "\n", encoding="utf-8")
    except: pass
    
    if total_scripts > 0 and success_count > 0:
        log_msg(f"[GÜNCELLEME] {success_count}/{total_scripts} eklenti basariyla senkronize edildi.")
        return True
    return False

# ══════════════════════════════════════════════════════════
#  PROTOKOL
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
    return 0, pos

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
#  PROXY: YÖNLENDİRİCİ
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
        except Exception as e: return None

    async def hot_swap(self, target_label):
        if self.current_label == target_label: return
        self.is_swapping = True
        self.play_state = False
        
        msg = json.dumps({"text": f"§a{target_label} sunucusuna geciliyor... Envanter senkronize ediliyor.", "color": "yellow"})
        self.client_w.write(pkt_make(0x02, mc_str_enc(msg) + bytes([0]), self.comp))
        await self.client_w.drain()

        if self.server_w:
            self.server_w.close()
            self.server_w = None
            self.server_r = None

        # DÜZELTME: Eski sunucunun sıkıştırma eşiğini sıfırla.
        # Sıfırlanmazsa yeni sunucudan gelen ham paketler yanlış
        # ayrıştırılır ve bağlantı anında kopar.
        self.comp = -1
            
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
                log_msg(f"[JOIN] {self.username} -> {self.current_label} sunucusuna girdi.")
                
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
#  HTTP API VE JSON PANEL EKLENTİSİ
# ══════════════════════════════════════════════════════════

class HttpHandler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            try:
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT label, players, host, port, last_seen FROM servers WHERE (? - last_seen) < 45 ORDER BY label ASC", (int(time.time()),))
                servers = cur.fetchall()
                conn.close()
            except: servers = []
            addr = _proxy_bore_addr if _proxy_bore_addr else "Tünel bekleniyor..."
            rows = ""
            for s in servers:
                rows += f"""
                <tr>
                  <td><span class="badge">{'🌐 HUB' if s['host']=='127.0.0.1' else '🎮 GS'}</span> {s['label']}</td>
                  <td>{s['host']}:{s['port']}</td>
                  <td><span class="players">👥 {s['players']}</span></td>
                  <td>
                    <button class="btn btn-warn" onclick="restartOne('{s['label']}')">🔄 Yeniden Başlat</button>
                  </td>
                </tr>"""
            html = f"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WC Network Panel</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;min-height:100vh;padding:24px; padding-bottom:50px;}}
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
  .btn-danger{{background:#da3633;color:#fff}} .btn-danger:hover{{background:#f85149}}
  .btn-warn{{background:#9e6a03;color:#fff}} .btn-warn:hover{{background:#d29922}}
  .btn-success{{background:#238636;color:#fff}} .btn-success:hover{{background:#2ea043}}
  .btn-warn:disabled,.btn-danger:disabled,.btn-success:disabled{{opacity:.4;cursor:not-allowed}}
  .actions{{display:flex;gap:10px;align-items:center;margin-bottom:20px}}
  #toast{{position:fixed;bottom:24px;right:24px;background:#238636;color:#fff;padding:12px 20px;border-radius:8px;font-size:.85rem;display:none;z-index:99;border:1px solid #2ea043}}
  #toast.err{{background:#da3633;border-color:#f85149}}
  .section-title{{font-size:.8rem;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px; margin-top:30px;}}
  
  .console-box {{background:#000; border:1px solid #30363d; border-radius:8px; height:350px; overflow-y:auto; padding:12px; font-family:monospace; color:#3fb950; font-size:13px; line-height:1.4; margin-bottom:12px;}}
  .console-input-row {{display:flex; gap:10px;}}
  .console-input {{flex:1; background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px; color:#fff; font-family:monospace; outline:none;}}
  .console-input:focus {{border-color:#58a6ff;}}
</style></head><body>
<h1>⛏️ WC Network Panel</h1>
<p class="subtitle">Minecraft Bungee Network Yönetim Paneli</p>
<div class="cards">
  <div class="card"><div class="val">{len(_active_players)}</div><div class="lbl">Aktif Oyuncu</div></div>
  <div class="card"><div class="val">{len(servers)}</div><div class="lbl">Aktif Sunucu</div></div>
  <div class="card"><div class="val">{'🟢' if servers else '🔴'}</div><div class="lbl">Ağ Durumu</div></div>
</div>
<div class="addr"><span>Minecraft Bağlantı Adresi</span>{addr}</div>

<div class="section-title">Sunucular</div>
<div class="actions">
  <button class="btn btn-danger" id="restartAllBtn" onclick="restartAll()">🔄 Tüm Sunucuları Yeniden Başlat</button>
  <button class="btn btn-success" id="updateScriptsBtn" onclick="updateScripts()">📥 Ağı Güncelle (GitHub Dinamik Liste)</button>
  <span id="statusMsg" style="color:#8b949e;font-size:.82rem"></span>
</div>
<table>
  <thead><tr><th>Sunucu</th><th>Adres</th><th>Oyuncu</th><th>İşlem</th></tr></thead>
  <tbody id="serverBody">{rows if rows else '<tr><td colspan="4" style="color:#8b949e;text-align:center;padding:28px">Aktif sunucu yok</td></tr>'}</tbody>
</table>

<div class="section-title">🖥️ Canlı Sistem Konsolu (Ağdaki Tüm Sunucular)</div>
<div class="console-box" id="consoleBox">Yükleniyor...</div>
<div class="console-input-row">
    <input type="text" class="console-input" id="cmdInput" placeholder="Komut yazın... (Örn: say Merhaba veya time set day)">
    <button class="btn btn-warn" onclick="sendCommand()">Gönder</button>
</div>

<div id="toast"></div>
<script>
function toast(msg,err=false){{
  const t=document.getElementById('toast');
  t.textContent=msg; t.className=err?'err':''; t.style.display='block';
  setTimeout(()=>t.style.display='none',3500);
}}
async function restartAll(){{
  const btn=document.getElementById('restartAllBtn');
  const msg=document.getElementById('statusMsg');
  btn.disabled=true; msg.textContent='Yeniden başlatma sinyali gönderiliyor...';
  try{{
    const r=await fetch('/api/restart_all',{{method:'POST'}});
    const d=await r.json();
    toast('✅ '+d.message); msg.textContent='Sinyal gönderildi!';
  }}catch(e){{toast('❌ Hata: '+e,true); msg.textContent='';}}
  setTimeout(()=>{{btn.disabled=false;msg.textContent='';}},5000);
}}
async function restartOne(label){{
  if(!confirm(label+' sunucusunu yeniden başlatmak istiyor musunuz?'))return;
  try{{
    const r=await fetch('/api/restart?label='+encodeURIComponent(label),{{method:'POST'}});
    const d=await r.json();
    toast('✅ '+d.message);
  }}catch(e){{toast('❌ Hata: '+e,true);}}
}}

async function updateScripts(){{
  const btn=document.getElementById('updateScriptsBtn');
  btn.disabled=true; toast('Tüm ağ GitHub üzerinden güncelleniyor...');
  try{{
    const r=await fetch('/api/update_scripts',{{method:'POST'}});
    const d=await r.json();
    if(d.ok) toast('✅ '+d.message);
    else toast('❌ '+d.message, true);
  }}catch(e){{toast('❌ Hata: '+e,true);}}
  setTimeout(()=>{{btn.disabled=false;}},4000);
}}

let autoScroll = true;
const cb = document.getElementById('consoleBox');
cb.addEventListener('scroll', () => {{
    if(cb.scrollTop + cb.clientHeight >= cb.scrollHeight - 20) autoScroll = true;
    else autoScroll = false;
}});

async function fetchLogs() {{
    try {{
        const r = await fetch('/api/logs');
        const d = await r.json();
        cb.innerHTML = d.logs.join('<br>') || "Konsol gecmisi bos...";
        if(autoScroll) cb.scrollTop = cb.scrollHeight;
    }} catch(e){{}}
}}
setInterval(fetchLogs, 1500);
fetchLogs();

document.getElementById('cmdInput').addEventListener('keypress', function(e) {{
    if (e.key === 'Enter') sendCommand();
}});

async function sendCommand() {{
    const input = document.getElementById('cmdInput');
    const cmd = input.value.trim();
    if(!cmd) return;
    input.value = '';
    try {{
        await fetch('/api/command', {{
            method:'POST', 
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{command: cmd}})
        }});
        toast('✅ Tüm sunuculara iletildi!');
        fetchLogs();
    }} catch(e){{toast('❌ Gönderim hatası', true);}}
}}
</script></body></html>"""
            self.wfile.write(html.encode('utf-8'))
            return

        if self.path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"logs": list(SYSTEM_LOGS)}).encode('utf-8'))
            return

        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            try:
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT label, players FROM servers WHERE (? - last_seen) < 45 ORDER BY label ASC", (int(time.time()),))
                servers = [{"sunucu": r["label"], "oyuncu_sayisi": r["players"]} for r in cur.fetchall()]
                conn.close()
            except: servers = []
            response = {
                "sistem": "WC Bungee Network Aktif",
                "minecraft_baglanti_adresi": _proxy_bore_addr if _proxy_bore_addr else "Tunnel baglantisi bekleniyor...",
                "toplam_oyuncu": len(_active_players),
                "aktif_sunucular": servers
            }
            self.wfile.write(json.dumps(response, indent=4).encode('utf-8'))
            return

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
            if MODE == "gameserver":
                proxy_url = os.environ.get("PROXY_URL", "")
                if proxy_url:
                    try:
                        req = urllib.request.Request(f"{proxy_url}/api/servers")
                        resp = urllib.request.urlopen(req, timeout=15).read()
                        self.send_response(200); self.end_headers(); self.wfile.write(resp)
                    except Exception as e:
                        self.send_response(500); self.end_headers()
                return

            try:
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("SELECT label, players FROM servers WHERE (? - last_seen) < 45 ORDER BY label ASC", (int(time.time()),))
                resp = ";".join([f"{r['label']}:{r['players']}" for r in cur.fetchall()])
                conn.close()
                self.send_response(200); self.end_headers(); self.wfile.write(resp.encode())
            except Exception: self.send_response(500); self.end_headers()

    def do_POST(self):
        global _cmd_counter, _last_script_update

        # YENİ SCRIPT GÜNCELLEME ENDPOINT'İ (Dinamik Liste Üzerinden)
        if self.path == "/api/update_scripts":
            success = update_and_configure()
            if success:
                _last_script_update = time.time()
                _write_to_cuberite("reload")
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "message": "Yeni Scriptler Listeden çekildi ve ağa iletiliyor!"}).encode())
            else:
                self.send_response(500); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "message": "Listeden scriptleri çekerken sorun yaşandı."}).encode())
            return
            
        if self.path == "/api/command":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
                cmd = data.get("command", "")
                if cmd:
                    _cmd_counter += 1
                    _cmd_history.append({"id": _cmd_counter, "cmd": cmd})
                    if len(_cmd_history) > 100: _cmd_history.pop(0)

                    _write_to_cuberite(cmd)
                    log_msg(f"[WEB-KOMUT] {cmd} (Ağdaki tüm sunuculara iletiliyor...)")
                    self.send_response(200)
                else:
                    self.send_response(400)
            except Exception as e:
                log_msg(f"[WEB-KOMUT HATA] {e}")
                self.send_response(500)
            self.end_headers()
            return
            
        if self.path == "/api/node_sync":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
                srv_id = data.get("server_id", "UNKNOWN")
                
                label = srv_id[:6]
                with _DB_LOCK:
                    try:
                        conn = sqlite3.connect(DB_FILE)
                        cur = conn.cursor()
                        cur.execute("SELECT label FROM servers WHERE server_id=?", (srv_id,))
                        row = cur.fetchone()
                        if row: label = row[0]
                        conn.close()
                    except: pass
                
                for l in data.get("logs", []):
                    SYSTEM_LOGS.append(f"[{label}] {l.split('] ', 1)[-1]}")

                client_cmd_id = data.get("last_cmd_id", 0)
                client_update_ts = data.get("last_update_ts", 0)

                new_cmds = [c for c in _cmd_history if c["id"] > client_cmd_id]
                needs_update = _last_script_update > client_update_ts

                resp = {
                    "commands": new_cmds,
                    "update_scripts": needs_update,
                    "current_ts": _last_script_update
                }

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
            return

        if self.path == "/api/restart_all":
            count = 0
            try:
                with _DB_LOCK:
                    conn = sqlite3.connect(DB_FILE)
                    cur = conn.cursor()
                    cur.execute("UPDATE servers SET restart_pending=1 WHERE (? - last_seen) < 45", (int(time.time()),))
                    count = cur.rowcount
                    conn.commit(); conn.close()
                _restart_local_cuberite()
                log_msg(f"[ADMIN] Tüm sunuculara ({count}) yeniden başlatma sinyali gönderildi.")
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "message": f"{count} sunucuya yeniden başlatma sinyali gönderildi."}).encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "message": str(e)}).encode())
            return

        if self.path.startswith("/api/restart"):
            label = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('label', [''])[0]
            if not label:
                self.send_response(400); self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "message": "label parametresi eksik"}).encode())
                return
            try:
                with _DB_LOCK:
                    conn = sqlite3.connect(DB_FILE)
                    conn.execute("UPDATE servers SET restart_pending=1 WHERE label=?", (label,))
                    conn.commit(); conn.close()
                if label in ("LOCAL_HUB_01", "GM1") or MODE == "all":
                    _restart_local_cuberite()
                log_msg(f"[ADMIN] '{label}' sunucusuna yeniden başlatma sinyali gönderildi.")
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "message": f"'{label}' sunucusuna sinyal gönderildi."}).encode())
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "message": str(e)}).encode())
            return

        elif self.path.startswith("/api/player_file"):
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
                server_id = s_data.get('server_id')
                now = int(time.time())
                
                with _DB_LOCK: 
                    conn = sqlite3.connect(DB_FILE)
                    cur = conn.cursor()
                    
                    if server_id:
                        cur.execute("SELECT label, restart_pending FROM servers WHERE server_id=?", (server_id,))
                        row = cur.fetchone()
                        if row:
                            label = row[0]
                            restart_needed = bool(row[1])
                            conn.execute("UPDATE servers SET host=?, port=?, last_seen=?, restart_pending=0 WHERE label=?", (host, port, now, label))
                        else:
                            restart_needed = False
                            cur.execute("SELECT COUNT(*) FROM servers")
                            label = f"GM{cur.fetchone()[0] + 1}"
                            try:
                                conn.execute("INSERT INTO servers (label, server_id, host, port, last_seen) VALUES (?, ?, ?, ?, ?)", (label, server_id, host, port, now))
                            except:
                                conn.execute("INSERT INTO servers (label, host, port, last_seen) VALUES (?, ?, ?, ?)", (label, host, port, now))
                    else:
                        restart_needed = False
                        cur.execute("SELECT COUNT(*) FROM servers")
                        label = f"GM{cur.fetchone()[0] + 1}"
                        conn.execute("INSERT INTO servers (label, host, port, last_seen) VALUES (?, ?, ?, ?)", (label, host, port, now))
                        
                    conn.commit(); conn.close()
                
                self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
                self.wfile.write(json.dumps({"label": label, "restart": restart_needed}).encode())
                if restart_needed:
                    log_msg(f"[REG] {label} sunucusuna restart komutu iletildi.")
            except Exception as e:
                self.send_response(500); self.end_headers()

    def log_message(self, format, *args): pass

# ══════════════════════════════════════════════════════════
#  BAŞLATICI YÖNTEMLER
# ══════════════════════════════════════════════════════════

def _restart_local_cuberite():
    global _cuberite_proc
    if _cuberite_proc and _cuberite_proc.poll() is None:
        try:
            _cuberite_proc.terminate()
            _cuberite_proc.wait(timeout=8)
        except Exception:
            try: _cuberite_proc.kill()
            except: pass
        _cuberite_proc = None

def run_http():
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    for _ in range(5):
        try:
            srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)
            srv.serve_forever()
            break
        except OSError: time.sleep(2)

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
                    log_msg(f"[BORE] Ana Yönlendirici Tüneli Açıldı! Adres: {_proxy_bore_addr}")
            proc.wait()
        except: pass
        time.sleep(5)

def run_bore_for_gameserver():
    global _pending_remote_logs, _cuberite_proc
    import re
    proxy_url = os.environ.get("PROXY_URL", "")
    if not proxy_url: return
    current_gs_bore = None
    
    server_id_file = f"{DATA_DIR}/server_id.txt"
    if os.path.exists(server_id_file):
        server_id = open(server_id_file).read().strip()
    else:
        import uuid; server_id = str(uuid.uuid4())
        pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
        open(server_id_file, "w").write(server_id)
    
    def heartbeat():
        while True:
            time.sleep(15)
            if current_gs_bore:
                try:
                    host, port_str = current_gs_bore.split(":")
                    payload = json.dumps({"host": host, "port": int(port_str), "server_id": server_id})
                    req = urllib.request.Request(f"{proxy_url}/api/register", data=payload.encode(), headers={"Content-Type": "application/json"})
                    resp = urllib.request.urlopen(req, timeout=5).read()
                    resp_data = json.loads(resp)
                    if resp_data.get("restart"): _restart_local_cuberite()
                except Exception: pass

    def sync_loop():
        global _pending_remote_logs
        last_cmd_id = 0
        last_update_ts = time.time()

        while True:
            time.sleep(1.5)
            with _LOG_LOCK:
                logs_to_send = list(_pending_remote_logs)
                _pending_remote_logs.clear()

            payload = {
                "server_id": server_id,
                "logs": logs_to_send,
                "last_cmd_id": last_cmd_id,
                "last_update_ts": last_update_ts
            }

            try:
                req = urllib.request.Request(f"{proxy_url}/api/node_sync",
                                             data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=5).read()
                data = json.loads(resp)

                for cmd_obj in data.get("commands", []):
                    cid = cmd_obj["id"]
                    cmd = cmd_obj["cmd"]
                    if cid > last_cmd_id:
                        last_cmd_id = cid
                        _write_to_cuberite(cmd)

                if data.get("update_scripts") and data.get("current_ts") > last_update_ts:
                    last_update_ts = data.get("current_ts")
                    log_msg("[SYNC] Merkezden guncelleme sinyali alindi! GitHub'dan cekiliyor...")
                    update_and_configure()
                    _write_to_cuberite("reload")

            except Exception as e:
                with _LOG_LOCK:
                    for l in reversed(logs_to_send):
                        _pending_remote_logs.insert(0, l)

    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()

    while True:
        try:
            proc = subprocess.Popen(["bore", "local", str(CUBERITE_PORT), "--to", "bore.pub"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = re.sub(r'\x1b\[[0-9;]*[mK]|\x1b\[\d*[A-Za-z]|\x1b\(\w', '', line.rstrip())
                if not line: continue
                m = re.search(r"bore\.pub:(\d+)", line)
                if m:
                    current_gs_bore = f"bore.pub:{m.group(1)}"
                    log_msg(f"[BORE] Alt Sunucu Tüneli: {current_gs_bore}")
            proc.wait()
        except: pass
        time.sleep(5)

def run_cuberite():
    update_and_configure() # Baslarken github'daki dinamik listeden son eklentileri ceker
    mc_bin = next(iter(glob.glob("/server/**/Cuberite", recursive=True)), None)
    if not mc_bin: return
    os.chmod(mc_bin, 0o755)
    
    persistent_world = f"{DATA_DIR}/world" if MODE == "all" else "/server/world"
    proxy_url = os.environ.get("PROXY_URL", f"http://127.0.0.1:{HTTP_PORT}")
    
    def _pipe_output(stream, proc):
        for raw in stream:
            line = raw.rstrip() if isinstance(raw, str) else raw.decode("utf-8", "replace").rstrip()
            if not line: continue
            
            log_msg(f"[CUBERITE] {line}")
            
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
                        _write_to_cuberite(f"wcreload {name}")
                    except Exception as e: pass
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
                    except Exception as e: pass
                threading.Thread(target=_do_upload, args=(line,), daemon=True).start()

    while True:
        global _cuberite_proc
        proc = subprocess.Popen([mc_bin], cwd=str(pathlib.Path(mc_bin).parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        _cuberite_proc = proc
        threading.Thread(target=_pipe_output, args=(proc.stdout, proc), daemon=True).start()
        proc.wait(); _cuberite_proc = None; time.sleep(5)

def register_local_cuberite():
    while True:
        time.sleep(15)
        try:
            payload = json.dumps({"host": "127.0.0.1", "port": CUBERITE_PORT, "server_id": "LOCAL_HUB_01"})
            req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/api/register", data=payload.encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception: pass

async def run_proxy():
    await init_db()
    server = await asyncio.start_server(handle_player, "0.0.0.0", MC_PORT)
    log_msg(f"[PROXY] Hub Yönlendirici {MC_PORT} portunda hazir...")
    async with server: await server.serve_forever()

def main():
    log_msg(f"""
+--------------------------------------------------+
|  Minecraft Bungee Network & Anti-Dupe Engine     |
|  Mod: {MODE:<43}|
+--------------------------------------------------+""")

    if MODE == "proxy":
        threading.Thread(target=run_http, daemon=True).start()
        threading.Thread(target=run_bore_for_proxy, daemon=True).start()
        asyncio.run(run_proxy())
        
    elif MODE == "gameserver":
        threading.Thread(target=run_http, daemon=True).start()
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
