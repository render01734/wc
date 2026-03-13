"""
⛏️  Minecraft Yönetim Paneli — v14.0 (Cuberite)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v10.0: NBD kaldırıldı → Resource Pool entegrasyonu
  - /api/agent/register + heartbeat
  - RAM Cache stats, flush
  - File Store (region arşiv)
  - CPU Worker görev gönderimi
  - TCP Proxy yönetimi
  - Panel'e "Kaynak Havuzu" sayfası eklendi
"""

import os, sys, json, time, threading, subprocess, shutil, zipfile
import re, glob
import urllib.request as _urllib_req
import urllib.parse as _urllib_parse
import urllib.error
import ssl as _ssl_mod
from collections import deque
from datetime import datetime
from pathlib import Path

import eventlet
eventlet.monkey_patch()

import resource as _resource
try:
    _PANEL_LIMIT = 480 * 1024 * 1024
    _s, _h = _resource.getrlimit(_resource.RLIMIT_AS)
    if _h == _resource.RLIM_INFINITY or _h > _PANEL_LIMIT:
        _resource.setrlimit(_resource.RLIMIT_AS, (_PANEL_LIMIT, _PANEL_LIMIT))
except Exception:
    pass

from flask import Flask, request, jsonify, send_file, abort, Response
from cluster import vcluster, cluster_api
from flask_socketio import SocketIO, emit

# ── Ayarlar ───────────────────────────────────────────────────
MC_DIR     = Path("/minecraft")
MC_BIN     = MC_DIR / "Cuberite"    # C++ binary, JVM yok
MC_PORT    = 25565
PANEL_PORT  = int(os.environ.get("PORT", "5000"))
# ── Çalışma Modu ─────────────────────────────────────────────────────────────
# MC_ONLY=1   → Flask/SocketIO yok. Sadece JVM + minimal HTTP API çalışır.
#               ~90MB kazanım → Xmx 280MB→370MB.
#               Panel UI başka bir agent'ta (WORKER_URL env ile) çalışır.
#
# WORKER_URL  → Bu process panel agent'i. JVM ana sunucuda (WORKER_URL).
#               start/stop/command → WORKER_URL'e proxy edilir.
MC_ONLY    = os.environ.get("MC_ONLY",    "0") == "1"
WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
IS_PROXY   = bool(WORKER_URL)
MC_VERSION = "1.8.8"  # Cuberite 1.8.8 uyumlu


# ── Global durum ─────────────────────────────────────────────
mc_process   = None
console_buf  = deque(maxlen=3000)
players      = {}
tunnel_info  = {"url": "", "host": ""}
_bootstrap_done = threading.Event()  # MC "Done!" gelince set edilir
server_state = {
    "status": "stopped", "tps": 20.0, "tps15": 20.0, "tps5": 20.0,
    "ram_mb": 0, "uptime": 0, "started": None,
    "version": "—", "max_players": 20, "online_players": 0,
}

# ── Resource Pool ─────────────────────────────────────────────
# key: node_id  val: {url, node_id, healthy, last_ping, info:{ram,disk,cpu,proxy}}
_agents: dict = {}
_agents_lock  = threading.Lock()

import gc as _gc_mod

app = Flask(__name__)
app.config["SECRET_KEY"] = "mc-panel-secret"

@app.after_request
def _gc_after(resp):
    _gc_mod.collect(0)
    return resp
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet",
                    ping_timeout=60, ping_interval=25)
if cluster_api: app.register_blueprint(cluster_api)
vcluster.set_socketio(socketio)


# ══════════════════════════════════════════════════════════════
#  YARDIMCI FONKSİYONLAR
# ══════════════════════════════════════════════════════════════

_spawn_emit_counter = 0   # spawn chunk log throttle sayacı

def log(line: str):
    global _spawn_emit_counter
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "line": line.rstrip()}
    console_buf.append(entry)

    # "Preparing spawn" satırlarını throttle et — her 20'de 1 emit et.
    # Aksi hâlde 400 chunk × hızlı akış → SocketIO kuyruğu dolup UI donuyor.
    if "Preparing spawn" in line or "chunks / sec" in line:
        _spawn_emit_counter += 1
        m = re.search(r'(\d+(?:\.\d+)?)%', line)
        pct = float(m.group(1)) if m else -1
        # Yalnızca %1, %25, %50, %75, %99+ ve her 20. satırı emit et
        if pct not in (-1,) and pct < 99 and _spawn_emit_counter % 20 != 0:
            _parse_mc_output(line)  # parse et ama emit etme
            return
        _spawn_emit_counter = 0
    else:
        _spawn_emit_counter = 0

    socketio.emit("console_line", entry)
    _parse_mc_output(line)


def _parse_mc_output(line: str):
    # ── Cuberite 1.8.8 log formatı ──────────────────────────────
    # Giriş:  "Player <name> has connected from <ip>"
    # Çıkış:  "Player <name> has disconnected"
    # Hazır:  "Cuberite 1.x.x is running"
    # TPS:    Cuberite kendi TPS'ini loglamaz — psutil ile ölçülür

    m = re.search(r"Player (\w+) has connected", line)
    if m:
        players[m.group(1)] = {"op": False, "joined_ts": time.time()}
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list())
        socketio.emit("stats_update", server_state)
        return

    m = re.search(r"Player (\w+) has disconnected", line)
    if m:
        players.pop(m.group(1), None)
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list())
        socketio.emit("stats_update", server_state)
        return

    # Paper uyumluluk (fallback):
    m2 = re.search(r"(\w+)\[/.+\] logged in", line)
    if m2:
        players[m2.group(1)] = {"op": False, "joined_ts": time.time()}
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list()); return

    m2 = re.search(r"(\w+) left the game|(\w+) lost connection", line)
    if m2:
        players.pop(m2.group(1) or m2.group(2), None)
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list()); return

    # Cuberite hazır sinyali: "Cuberite 1.x is running" veya "Startup complete"
    if re.search(r"Cuberite .* is running|Startup complete|server is running", line, re.I):
        server_state["status"]  = "running"
        server_state["version"] = "1.8.8"
        server_state["started"] = time.time()
        server_state["online_players"] = 0
        try: socketio.emit("server_status", server_state)
        except Exception: pass
        log("[Panel] ✅ Cuberite Server hazır! (1.8.8)")
        threading.Thread(target=_tps_monitor, daemon=True).start()
        try: _bootstrap_done.set()
        except Exception: pass

    # Paper/Paper uyumlu "Done" sinyali (fallback)
    if "Done" in line and "help" in line.lower():
        server_state["status"]  = "running"
        server_state["started"] = time.time()
        try: socketio.emit("server_status", server_state)
        except Exception: pass
        log("[Panel] ✅ Server hazır!")
        threading.Thread(target=_tps_monitor, daemon=True).start()
        try: _bootstrap_done.set()
        except Exception: pass

    # iostream error → dizin oluştur + stats dosyasını hazırla
    if ("iostream error" in line or "statistics file loading failed" in line
            or "save or statistics file loading failed" in line):
        threading.Thread(target=_ensure_runtime_dirs, daemon=True).start()
        m_pname = re.search(r'Player "([A-Za-z0-9_]+)"', line)
        if m_pname:
            threading.Thread(
                target=_pre_create_player_files, args=(m_pname.group(1),), daemon=True
            ).start()

    # "prevented from joining" → stats dosyası yoktu, şimdi oluştur
    # Oyuncu TEKRAR bağlanınca sorunsuz girer
    if "prevented from joining" in line or "could not be parsed" in line:
        m_p2 = re.search(r'Player "([A-Za-z0-9_]+)"', line)
        if m_p2:
            threading.Thread(
                target=_pre_create_player_files, args=(m_p2.group(1),), daemon=True
            ).start()
            log(f"[Players] 🔄 {m_p2.group(1)} → dosyalar hazırlandi, yeniden baglanabilir")

    if "Stopping server" in line or "Shutting down" in line:
        server_state["status"] = "stopping"
        socketio.emit("server_status", server_state)


def _players_list():
    return [{"name": n, **info} for n, info in players.items()]



def _pre_create_player_files(player_name: str):
    """
    Oyuncu için SADECE DİZİN oluştur + izin ver.
    ⚠️  Dosya YARATMA — Cuberite player/stats dosyalarını kendi oluşturur.
    Boş '{}' yazmak 'basic_ios::clear: iostream error' hatasına yol açar
    çünkü Cuberite beklediği alanları bulamayınca stream exception fırlatır.
    """
    if not player_name:
        return
    import subprocess as _spp
    # SADECE dizinler — dosya oluşturma!
    for _d in [
        MC_DIR / "players",
        MC_DIR / "world" / "data" / "stats",
        MC_DIR / "world" / "playerdata",
        MC_DIR / "world" / "players",
    ]:
        try:
            _d.mkdir(parents=True, exist_ok=True)
            _spp.run(["chmod", "777", str(_d)], capture_output=True, timeout=2)
        except Exception:
            pass

    # Eğer stats dosyası bozuksa (var ama okunemiyor) SİL — {} yazma!
    # Dosya yoksa: Cuberite "not found, resetting to defaults" deyip geçer → sorun yok
    # Dosya varsa ama bozuksa: Cuberite basic_ios::clear fırlatır → SİL ki "not found" alsın
    _stats_file = MC_DIR / "world" / "data" / "stats" / f"{player_name}.json"
    if _stats_file.exists():
        try:
            import json as _jj
            _jj.loads(_stats_file.read_text())
        except Exception:
            try:
                _stats_file.unlink()
                log(f"[Players] {player_name} bozuk stats dosyası silindi")
            except Exception: pass

    log(f"[Players] ✅ {player_name} dizin+izin hazir — yeniden baglanabilir")


def _reset_player_files(player_name: str):
    """
    ⚠️  OYUNCU DOSYASI HİÇBİR ZAMAN SİLİNMEZ — envanter/konum/can korunur.
    Sadece: dizinleri oluştur + izin ver.
    ⚠️  Boş '{}' YAZILMAZ — Cuberite player/stats dosyalarını kendi oluşturur.
    Boş dosya yazmak basic_ios::clear hatasına yol açar.
    """
    import subprocess as _sp
    for _d in [
        MC_DIR / "players",
        MC_DIR / "world" / "data" / "stats",
        MC_DIR / "world" / "playerdata",
        MC_DIR / "world" / "players",
    ]:
        _d.mkdir(parents=True, exist_ok=True)
        try: _sp.run(["chmod", "777", str(_d)], capture_output=True, timeout=3)
        except Exception: pass

    if not player_name:
        return

    # Stats dosyası bozuksa SİL (Cuberite "not found" durumunu graceful handle eder)
    # ASLA {} yazma — Cuberite stream okurken beklediği yapıyı bulamazsa exception fırlatır
    import json as _jreset
    for fp in [
        MC_DIR / "world" / "data" / "stats" / f"{player_name}.json",
    ]:
        if fp.exists():
            try:
                _jreset.loads(fp.read_text())
            except Exception:
                try:
                    fp.unlink()
                    log(f"[Players] {player_name} bozuk stats silindi — Cuberite yeniden olusturacak")
                except Exception: pass

    log(f"[Players] ✅ {player_name} dizin+izin hazir — yeniden baglanabilir")

def _ensure_runtime_dirs():
    """Cuberite için gerekli dizinleri garantile ve izin ver."""
    import subprocess as _sp3, struct as _st, gzip as _gz, io as _io

    _all_dirs = [
        MC_DIR / "players",
        MC_DIR / "players_backup",
        MC_DIR / "players_corrupted",
        MC_DIR / "world" / "data",
        MC_DIR / "world" / "data" / "stats",
        MC_DIR / "world" / "playerdata",
        MC_DIR / "world" / "players",
        MC_DIR / "world" / "region",
        MC_DIR / "world_nether" / "data",
        MC_DIR / "world_nether" / "data" / "stats",
        MC_DIR / "world_nether" / "DIM-1" / "region",
        MC_DIR / "world_the_end" / "data",
        MC_DIR / "world_the_end" / "data" / "stats",
        MC_DIR / "world_the_end" / "DIM1" / "region",
        MC_DIR / "logs",
        MC_DIR / "crash-reports",
    ]
    for _d in _all_dirs:
        _d.mkdir(parents=True, exist_ok=True)
        try: _sp3.run(["chmod", "777", str(_d)], capture_output=True, timeout=2)
        except Exception: pass

    # scoreboard.dat — gzip+NBT binary (her world için, yoksa oluştur)
    def _write_sbd(p):
        def _s(t): b=t.encode(); return _st.pack('>H',len(b))+b
        def _l(n,e,c): return bytes([9])+_s(n)+bytes([e])+_st.pack('>i',c)
        def _c(n,pl): return bytes([10])+_s(n)+pl+bytes([0])
        root = bytes([10])+_s('')+_c('data',_l('Objectives',10,0)+_l('PlayerScores',10,0)+_l('Teams',10,0)+_c('DisplaySlots',b''))+bytes([0])
        buf=_io.BytesIO()
        with _gz.GzipFile(fileobj=buf,mode='wb',mtime=0) as gz: gz.write(root)
        p.write_bytes(buf.getvalue())

    for _w in ("world", "world_nether", "world_the_end"):
        _sbd = MC_DIR / _w / "data" / "scoreboard.dat"
        if not _sbd.exists():
            try: _write_sbd(_sbd); log(f"[Panel] {_w}/data/scoreboard.dat yazildi (NBT)")
            except Exception as _e: log(f"[Panel] scoreboard hata ({_w}): {_e}")
        try: _sp3.run(["chmod","666",str(_sbd)], capture_output=True, timeout=2)
        except Exception: pass
def _backup_players_to_agents():
    """players/*.json dosyalarini agent disk'e yedekle."""
    players_dir = MC_DIR / "players"
    agents = [a for a in _agents.values() if a.get("healthy")]
    if not agents or not players_dir.exists():
        return 0
    best = max(agents,
               key=lambda a: a.get("info", {}).get("disk", {}).get("store_free_gb", 0),
               default=None)
    if not best:
        return 0
    backed = 0
    for pf in players_dir.glob("*.json"):
        if not pf.is_file() or pf.stat().st_size == 0:
            continue
        try:
            data = pf.read_bytes()
            req  = _urllib_req.Request(
                best["url"] + f"/api/files/players/{pf.name}",
                data=data, method="PUT",
                headers={"Content-Type": "application/octet-stream"})
            _urllib_req.urlopen(req, timeout=30)
            backed += 1
        except Exception: pass
    if backed:
        log(f"[Players] {backed} dosya agent'a yedeklendi")
    return backed


def _tps_monitor():
    """CPU'dan TPS tahmini + 5 dakikada bir player yedegi."""
    import psutil
    _last_backup = [0.0]
    while mc_process and mc_process.poll() is None:
        time.sleep(10)
        if server_state["status"] == "running":
            try:
                proc  = psutil.Process(mc_process.pid)
                cpu_p = proc.cpu_percent(interval=1)
                tps_est = round(max(1.0, min(20.0, 20.0 * (1.0 - cpu_p / 100.0))), 1)
                server_state["tps"] = tps_est
                server_state["tps5"] = tps_est
                server_state["tps15"] = tps_est
                socketio.emit("stats_update", server_state)
            except Exception:
                pass
            _now = time.time()
            if _now - _last_backup[0] > 300:
                try:
                    _backup_players_to_agents()
                    _last_backup[0] = _now
                except Exception: pass


def _stdout_reader():
    global mc_process
    while mc_process and mc_process.poll() is None:
        try:
            line = mc_process.stdout.readline()
            if line:
                log(line.decode("utf-8", errors="replace"))
        except Exception:
            break
    server_state["status"] = "stopped"
    server_state["online_players"] = 0
    players.clear()
    socketio.emit("server_status", server_state)
    socketio.emit("players_update", [])
    log("[Panel] 🔴 Minecraft Server durduruldu.")


def _ram_monitor():
    import psutil
    while True:
        eventlet.sleep(5)
        if mc_process and mc_process.poll() is None:
            try:
                proc = psutil.Process(mc_process.pid)
                server_state["ram_mb"] = int(proc.memory_info().rss / 1024 / 1024)
                if server_state["started"]:
                    server_state["uptime"] = int(time.time() - server_state["started"])
                socketio.emit("stats_update", server_state)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════
#  RESOURCE POOL — Agent yönetimi
# ══════════════════════════════════════════════════════════════

def _pool_register(tunnel_url: str, node_id: str, info: dict):
    with _agents_lock:
        is_new = node_id not in _agents
        _agents[node_id] = {
            "url":       tunnel_url.rstrip("/"),
            "node_id":   node_id,
            "healthy":   True,
            "last_ping": time.time(),
            "connected_at": _agents.get(node_id, {}).get("connected_at", time.time()),
            "info":      info,
        }
    return is_new



def _agent_req(agent: dict, method: str, path: str,
               data: bytes = None, headers: dict = None, timeout: int = 15):
    try:
        req = _urllib_req.Request(
            agent["url"] + path,
            data=data,
            headers={"Content-Type": "application/octet-stream", **(headers or {})},
            method=method,
        )
        with _urllib_req.urlopen(req, timeout=timeout) as r:
            agent["healthy"]   = True
            agent["last_ping"] = time.time()
            return r.read()
    except Exception as e:
        agent["healthy"] = False
        return None


def _agent_json(agent: dict, method: str, path: str,
                body: dict = None, timeout: int = 15):
    raw = _agent_req(
        agent, method, path,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if raw:
        try: return json.loads(raw)
        except: pass
    return None


def _best_agent_by_disk() -> dict | None:
    with _agents_lock:
        agents = [a for a in _agents.values() if a["healthy"]]
    if not agents:
        return None
    return max(agents, key=lambda a: a["info"].get("disk", {}).get("free_gb", 0))


def _best_agent_by_ram() -> dict | None:
    with _agents_lock:
        agents = [a for a in _agents.values() if a["healthy"]]
    if not agents:
        return None
    return min(agents, key=lambda a: a["info"].get("ram", {}).get("cache_mb", 9999))


def _pool_health_watchdog():
    """Arka planda agent sağlığını izle."""
    while True:
        time.sleep(35)
        with _agents_lock:
            agents = list(_agents.values())
        for ag in agents:
            r = _agent_json(ag, "GET", "/api/status", timeout=8)
            if r:
                ag["info"]      = r
                ag["healthy"]   = True
                ag["last_ping"] = time.time()
            else:
                # 90sn yanıt yoksa unhealthy
                if time.time() - ag["last_ping"] > 90:
                    ag["healthy"] = False
        socketio.emit("pool_update", vcluster.summary())


def _pool_auto_optimize():
    """
    Periyodik olarak:
    1. Eski region'ları agent'a taşı → ana sunucuda disk aç → swap büyüt
    2. Düşük RAM'de JVM dışı cache devreye girer
    """
    time.sleep(120)   # Sunucu stabil olana kadar bekle
    while True:
        try:
            _auto_archive_old_regions()
        except Exception as e:
            log(f"[Pool] ⚠️  Otomatik arşiv hatası: {e}")
        time.sleep(600)   # 10 dakikada bir


def _auto_archive_old_regions(older_than_days: int = 5):
    import shutil as _sh
    best = _best_agent_by_disk()
    if not best:
        return
    # shutil.disk_usage("/") Render container'da HOST FS'i okur (~68GB free görünür).
    # Gerçek limit: /minecraft dizini kullanımı + 4GB OS tabanı.
    try:
        mc_used_gb = sum(
            f.stat().st_size for f in MC_DIR.rglob("*") if f.is_file()
        ) / 1e9
    except Exception:
        mc_used_gb = 0.0
    render_limit_gb = float(os.environ.get("RENDER_DISK_LIMIT_GB", "18.0"))
    used_gb = 4.0 + mc_used_gb
    if used_gb < render_limit_gb * 0.65:
        return  # Disk %65'ten az doluysa gerek yok

    archived  = 0
    freed_mb  = 0
    now       = time.time()

    for dim_dir in [MC_DIR / "world" / "region",
                    MC_DIR / "world_nether" / "DIM-1" / "region",
                    MC_DIR / "world_the_end" / "DIM1" / "region"]:
        if not dim_dir.exists():
            continue
        dim = dim_dir.parts[-3]   # world / world_nether / world_the_end

        for rf in sorted(dim_dir.glob("*.mca"), key=lambda f: f.stat().st_mtime):
            if (now - rf.stat().st_mtime) / 86400 < older_than_days:
                continue
            try:
                data = rf.read_bytes()
                url  = best["url"] + f"/api/files/regions/{dim}/{rf.name}"
                req  = _urllib_req.Request(url, data=data, method="PUT",
                                           headers={"Content-Type": "application/octet-stream"})
                _urllib_req.urlopen(req, timeout=120)
                rf.unlink()
                freed_mb += len(data) / 1e6
                archived  += 1
            except Exception as e:
                continue   # sessiz hata

    if archived > 0:
        # NOT: swapon Render'da EPERM → deneme yok. UserSwap 4GB dosya swap sağlıyor.
        log(f"[Pool] 💾 {archived} region arşivlendi ({freed_mb:.0f}MB) → agent disk")

    return archived, freed_mb



# ══════════════════════════════════════════════════════════════
#  AGENT AKTIF KULLLANIM — RAM Cache + Disk Sync Daemon'ları
#  ─────────────────────────────────────────────────────────────
#  _region_disk_daemon  → yeni/değişen .mca dosyaları agent disk'e iter
#  _region_cache_daemon → sık erişilen region'ları agent RAM'e cache'ler
#  _region_restore      → Cuberite başlamadan önce agent'tan geri yükle
# ══════════════════════════════════════════════════════════════

# Hangi region'lar agent disk'te (offloaded = yerel kopyası silindi)
_offloaded: set[str]  = set()   # "world/r.0.0.mca" gibi
_synced:    set[str]  = set()   # yerel + agent'ta eşzamanlı
_region_lock = threading.Lock()

# Agent disk'e gönder — PUT /api/files/regions/<dim>/<name>
def _push_region(rf: Path, dim: str, agent: dict, delete_local: bool = False) -> bool:
    try:
        data = rf.read_bytes()
        url  = agent["url"] + f"/api/files/regions/{dim}/{rf.name}"
        req  = _urllib_req.Request(
            url, data=data, method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        _urllib_req.urlopen(req, timeout=120)
        key  = f"{dim}/{rf.name}"
        with _region_lock:
            if delete_local:
                try: rf.unlink()
                except Exception: pass
                _offloaded.add(key)
                _synced.discard(key)
            else:
                _synced.add(key)
        return True
    except Exception as e:
        log(f"[RegionSync] ⚠️  {rf.name} → agent hatası: {e}")
        return False


# Agent disk'ten geri yükle — GET /api/files/regions/<dim>/<name>
def _restore_region(dim: str, name: str, agent: dict) -> bool:
    try:
        url = agent["url"] + f"/api/files/regions/{dim}/{name}"
        with _urllib_req.urlopen(url, timeout=60) as r:
            data = r.read()
        dest = MC_DIR / dim / "region" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        key = f"{dim}/{name}"
        with _region_lock:
            _offloaded.discard(key)
            _synced.add(key)
        return True
    except Exception:
        return False


# Agent RAM cache'e küçük/sık region bas
def _push_region_cache(rf: Path, dim: str, agent: dict) -> bool:
    if rf.stat().st_size > 4 * 1024 * 1024:  # 4MB'dan büyük → sadece disk
        return False
    try:
        data = rf.read_bytes()
        import gzip, base64
        compressed = gzip.compress(data, compresslevel=1)
        url  = agent["url"] + "/api/cache/set"
        body = {"key": f"region:{dim}:{rf.name}", "value": base64.b64encode(compressed).decode(),
                "ttl": 3600}
        req  = _urllib_req.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        _urllib_req.urlopen(req, timeout=30)
        return True
    except Exception:
        return False


def _region_disk_daemon():
    """
    Arka planda sürekli çalışır.
    Yeni veya değişen .mca region dosyalarını agent disk'e iter.
    Disk %60+ doluysa eski region'ları offload eder (yerel siler).
    """
    log("[RegionSync] 🟢 Disk sync daemon başladı")
    _seen_mtime: dict[str, float] = {}  # rf.key → son gönderilen mtime
    DIMS = [
        ("world",          MC_DIR / "world"          / "region"),
        ("world_nether",   MC_DIR / "world_nether"   / "region"),
        ("world_the_end",  MC_DIR / "world_the_end"  / "region"),
    ]

    while True:
        try:
            agents = [a for a in _agents.values() if a["healthy"]]
            if not agents:
                time.sleep(30)
                continue

            # En çok disk alanı olan agent'ı seç
            best = max(agents, key=lambda a: a["info"].get("disk", {}).get("store_free_gb", 0))
            if best["info"].get("disk", {}).get("store_free_gb", 0) < 0.1:
                time.sleep(60)
                continue

            # Disk kullanımını hesapla
            try:
                mc_used_gb = sum(f.stat().st_size for f in MC_DIR.rglob("*") if f.is_file()) / 1e9
            except Exception:
                mc_used_gb = 0.0
            disk_pct = (4.0 + mc_used_gb) / float(os.environ.get("RENDER_DISK_LIMIT_GB", "18")) * 100

            now = time.time()
            for dim, region_dir in DIMS:
                if not region_dir.exists():
                    continue
                for rf in sorted(region_dir.glob("*.mca"), key=lambda f: f.stat().st_mtime):
                    key  = f"{dim}/{rf.name}"
                    mt   = rf.stat().st_mtime
                    age_days = (now - mt) / 86400

                    # Değişmemişse ve zaten sync'se geç
                    if _seen_mtime.get(key) == mt and key in _synced:
                        # Disk %60+ doluysa ve 1 günden eskiyse offload et
                        if disk_pct > 60 and age_days > 1 and key not in _offloaded:
                            if _push_region(rf, dim, best, delete_local=True):
                                log(f"[RegionSync] 📦 Offload: {rf.name} ({rf.stat().st_size//1024}KB → agent disk, disk={disk_pct:.0f}%)")
                        continue

                    # Yeni veya değişmiş — agent disk'e gönder
                    # Aktif oyuncu bölgesi (son 10 dakika) → offload etme, sadece sync
                    delete = disk_pct > 60 and age_days > 0.5
                    if _push_region(rf, dim, best, delete_local=delete):
                        _seen_mtime[key] = mt
                        if delete:
                            log(f"[RegionSync] 📤 Sync+Offload: {rf.name}")
                        else:
                            log(f"[RegionSync] 📤 Sync: {rf.name} ({rf.stat().st_size//1024}KB)")

        except Exception as e:
            log(f"[RegionSync] ⚠️  Hata: {e}")

        time.sleep(15)  # 15 saniyede bir tara


def _region_cache_daemon():
    """
    Arka planda çalışır.
    Son 30 dakika içinde erişilen küçük region'ları agent RAM cache'e yükler.
    Cache hit → agent'tan, cache miss → diskten okur (Cuberite kendi okur).
    """
    log("[RegionCache] 🟢 RAM cache daemon başladı")
    DIMS = [
        ("world",         MC_DIR / "world"         / "region"),
        ("world_nether",  MC_DIR / "world_nether"  / "region"),
        ("world_the_end", MC_DIR / "world_the_end" / "region"),
    ]
    _cached_keys: set[str] = set()

    while True:
        try:
            agents = [a for a in _agents.values() if a["healthy"]]
            if not agents:
                time.sleep(60)
                continue

            # En çok boş RAM'li agent
            best_ram = max(agents, key=lambda a: a["info"].get("ram", {}).get("free_mb", 0))
            free_mb  = best_ram["info"].get("ram", {}).get("free_mb", 0)
            if free_mb < 50:
                time.sleep(60)
                continue

            now      = time.time()
            pushed   = 0
            for dim, region_dir in DIMS:
                if not region_dir.exists():
                    continue
                for rf in region_dir.glob("*.mca"):
                    key = f"region:{dim}:{rf.name}"
                    if key in _cached_keys:
                        continue
                    # Son 30 dakikada erişildiyse cache'e yükle
                    try:
                        atime = rf.stat().st_atime
                    except Exception:
                        atime = rf.stat().st_mtime
                    if (now - atime) < 1800:
                        if _push_region_cache(rf, dim, best_ram):
                            _cached_keys.add(key)
                            pushed += 1
                            free_mb -= rf.stat().st_size // (1024 * 1024)
                            if free_mb < 50 or pushed >= 20:
                                break
                if pushed >= 20 or free_mb < 50:
                    break

            if pushed > 0:
                log(f"[RegionCache] 🧠 {pushed} region agent RAM'e yüklendi")

        except Exception as e:
            log(f"[RegionCache] ⚠️  Hata: {e}")

        time.sleep(60)  # 1 dakikada bir


def _region_restore_all():
    """
    Cuberite başlamadan önce offload edilmiş region'ları geri yükle.
    Tüm agent'lardaki region listesini al ve gerekli olanları indir.
    """
    agents = [a for a in _agents.values() if a["healthy"]]
    if not agents:
        return
    log("[RegionSync] 🔄 Offload'lu region'lar geri yükleniyor...")
    restored = 0
    for agent in agents:
        try:
            for dim in ["world", "world_nether", "world_the_end"]:
                url  = agent["url"] + f"/api/files/regions/{dim}"
                try:
                    with _urllib_req.urlopen(url, timeout=15) as r:
                        files = json.loads(r.read())
                except Exception:
                    files = []
                for fname in files:
                    key  = f"{dim}/{fname}"
                    dest = MC_DIR / dim / "region" / fname
                    if not dest.exists():
                        if _restore_region(dim, fname, agent):
                            restored += 1
        except Exception:
            pass
    if restored > 0:
        log(f"[RegionSync] ✅ {restored} region geri yüklendi")

# ══════════════════════════════════════════════════════════════
#  MC SERVER YÖNETİMİ
# ══════════════════════════════════════════════════════════════

def download_cuberite() -> bool:
    """
    Cuberite C++ binary'yi indir ve /minecraft/Cuberite'e yerleştir.
    JVM jar değil — doğrudan çalıştırılabilir binary (~10MB).

    İndirme kaynakları (sırasıyla denenir):
      1. Cuberite CI nightly (Linux x86_64) — en güncel
      2. GitHub releases fallback
    """
    if MC_BIN.exists() and MC_BIN.stat().st_size > 500_000:
        log(f"[Panel] ✅ Cuberite binary zaten mevcut "
            f"({MC_BIN.stat().st_size // 1024 // 1024}MB)")
        MC_BIN.chmod(0o755)
        return True

    MC_DIR.mkdir(parents=True, exist_ok=True)

    URLS = [
        # 1. Resmi Cuberite download sunucusu (canonical URL — her zaman güncel)
        "https://download.cuberite.org/linux-x86_64/Cuberite.tar.gz",
        # 2. Builds.cuberite.org nightly (ikincil)
        "https://builds.cuberite.org/job/Cuberite%20Linux%20x64%20Master/"
        "lastSuccessfulBuild/artifact/Cuberite.tar.gz",
    ]

    import tarfile as _tf

    for url in URLS:
        try:
            log(f"[Panel] 📥 Cuberite indiriliyor: {url.split('/')[-1]}")
            server_state["status"] = "downloading"
            try:
                socketio.emit("server_status", server_state)
            except Exception:
                pass

            tmp = Path("/tmp/cuberite_dl.tar.gz")
            req = _urllib_req.Request(
                url, headers={"User-Agent": "MCPanel/14.0 Cuberite-Downloader"}
            )
            with _urllib_req.urlopen(req, timeout=180) as resp:
                tmp.write_bytes(resp.read())

            if not tmp.exists() or tmp.stat().st_size < 100_000:
                log(f"[Panel] ⚠️  Dosya çok küçük veya boş — sonraki URL deneniyor")
                tmp.unlink(missing_ok=True)
                continue

            log(f"[Panel] 📦 Arşiv açılıyor ({tmp.stat().st_size // 1024 // 1024}MB)...")
            with _tf.open(tmp, "r:gz") as tf:
                tf.extractall(str(MC_DIR))
            tmp.unlink(missing_ok=True)

            # Binary konumunu bul — tar yapısına göre farklı olabilir
            candidates = [
                MC_BIN,                          # /minecraft/Cuberite
                MC_DIR / "Server" / "Cuberite",  # /minecraft/Server/Cuberite
                MC_DIR / "Cuberite-Server" / "Cuberite",
            ]
            for candidate in candidates:
                if candidate.exists() and candidate.stat().st_size > 100_000:
                    candidate.chmod(0o755)
                    if candidate != MC_BIN:
                        import shutil as _shutil
                        _shutil.copy2(str(candidate), str(MC_BIN))
                        MC_BIN.chmod(0o755)
                        log(f"[Panel] ✅ Cuberite kopyalandı: {candidate} → {MC_BIN}")
                    else:
                        log(f"[Panel] ✅ Cuberite hazır: {MC_BIN}")
                    _patch_cuberite_data()
                    return True

            # Binary bulunamazsa tar içinde ara
            for f in MC_DIR.rglob("Cuberite"):
                if f.is_file() and f.stat().st_size > 100_000:
                    f.chmod(0o755)
                    import shutil as _shutil
                    _shutil.copy2(str(f), str(MC_BIN))
                    MC_BIN.chmod(0o755)
                    log(f"[Panel] ✅ Cuberite bulundu ve kopyalandı: {f.parent.name}/Cuberite")
                    _patch_cuberite_data()
                    return True

            log(f"[Panel] ⚠️  Cuberite binary arşiv içinde bulunamadı ({url})")

        except Exception as e:
            log(f"[Panel] ⚠️  İndirme hatası ({url.split('/')[-1]}): {e}")
            continue

    log("[Panel] ❌ Cuberite indirilemedi! Tüm kaynaklar denendi.")
    return False


def _patch_cuberite_data():
    """
    Cuberite indirildikten sonra bilinen uyarıları gidermek için data dosyalarını düzeltir.

    1. Protocol/1.13/base.recipes.txt  — Cuberite bu dosyayı içermiyor, boş oluştur
    2. Protocol/1.14.4/base.recipes.txt — aynı
    3. JungleTemple.cubeset — BambooJungle ve BambooJungleHills Cuberite'de yok, kaldır
    4. PlayerSave Lua plugin — HOOK_LOGIN ile stats dosyasını giriş öncesi hazırlar
    """
    import re as _re

    # ── 1 & 2: Eksik recipe dosyaları ──────────────────────────────
    for _proto_ver in ("1.13", "1.14.4"):
        _recipe_path = MC_DIR / "Protocol" / _proto_ver / "base.recipes.txt"
        if not _recipe_path.exists():
            try:
                _recipe_path.parent.mkdir(parents=True, exist_ok=True)
                _recipe_path.write_text("# Cuberite placeholder — protocol not fully supported\n")
                log(f"[Panel] Protocol/{_proto_ver}/base.recipes.txt oluşturuldu")
            except Exception as _e:
                log(f"[Panel] recipe placeholder yazılamadı ({_proto_ver}): {_e}")

    # ── 3: JungleTemple.cubeset — desteklenmeyen biyomları kaldır ──
    _cubeset = MC_DIR / "Prefabs" / "SinglePieceStructures" / "JungleTemple.cubeset"
    if _cubeset.exists():
        try:
            _txt = _cubeset.read_text()
            _original = _txt
            for _bm in ("BambooJungleHills", "BambooJungle"):
                _txt = _re.sub(r',\s*"' + _bm + r'"', '', _txt)
                _txt = _re.sub(r'"' + _bm + r'"\s*,\s*', '', _txt)
                _txt = _re.sub(r'"' + _bm + r'"', '', _txt)
            if _txt != _original:
                _cubeset.write_text(_txt)
                log("[Panel] JungleTemple.cubeset: BambooJungle biyomları kaldırıldı")
        except Exception as _e:
            log(f"[Panel] cubeset patch hatası: {_e}")

    # ── 4: PlayerSave Lua plugin — her zaman taze yaz ──────────────
    # HOOK_LOGIN (player entity oluşmadan önce) ile stats dosyasını hazırlar.
    # Cuberite {"stats":{}} formatını bekler — {} veya eksik dosya iostream hatası verir.
    _plugin_dir = MC_DIR / "Plugins" / "PlayerSave"
    _plugin_dir.mkdir(parents=True, exist_ok=True)
    _plugin_main = _plugin_dir / "main.lua"
    _plugin_main.write_text(r'''-- PlayerSave v1.0 — stats/player dosyası iostream hatasını önler
-- HOOK_LOGIN: player entity oluşmadan ÖNCE stats dosyasını hazırla
-- Cuberite {"stats":{}} formatını bekler; {} veya eksik dosya kick verir

local EMPTY_STATS = '{"stats":{}}'

local function EnsureDir(path)
    os.execute('mkdir -p "' .. path .. '" 2>/dev/null')
    os.execute('chmod 777 "' .. path .. '" 2>/dev/null')
end

local function IsValidStats(path)
    local f = io.open(path, "r")
    if not f then return false end
    local c = f:read("*all"); f:close()
    return c and c:find('"stats"') ~= nil
end

local function WriteStats(path, name)
    local f = io.open(path, "w")
    if f then
        f:write(EMPTY_STATS); f:close()
        os.execute('chmod 666 "' .. path .. '" 2>/dev/null')
        LOG("[PlayerSave] " .. name .. " stats hazir")
    end
end

function OnLogin(Client, ProtocolVersion, Username)
    if not Username or Username == "" then return false end
    local dir  = "world/data/stats"
    local path = dir .. "/" .. Username .. ".json"
    EnsureDir(dir)
    EnsureDir("players")
    if not IsValidStats(path) then
        local bak = dir .. "/_bak_" .. Username .. ".json"
        os.rename(path, bak)
        WriteStats(path, Username)
    end
    return false
end

function OnPlayerDestroyed(Player)
    local name = Player:GetName()
    local path = "world/data/stats/" .. name .. ".json"
    if not IsValidStats(path) then
        WriteStats(path, name)
    end
    return false
end

function Initialize(Plugin)
    Plugin:SetName("PlayerSave")
    Plugin:SetVersion(1)
    cPluginManager.AddHook(cPluginManager.HOOK_LOGIN,            OnLogin)
    cPluginManager.AddHook(cPluginManager.HOOK_PLAYER_DESTROYED, OnPlayerDestroyed)
    LOG("[PlayerSave] v1.0 aktif — HOOK_LOGIN + HOOK_PLAYER_DESTROYED")
    return true
end

function OnDisable()
    LOG("[PlayerSave] devre disi")
end
''')
    log("[Panel] PlayerSave Lua plugin yazıldı: Plugins/PlayerSave/main.lua")


def download_paper():
    """Geriye dönük uyumluluk — Cuberite C++ kullanılıyor."""
    return download_cuberite()

def _clean_player_files():
    """
    OYUNCU DOSYALARI ASLA SILINMEZ.
    Bozuk .json dosyalari players_corrupted/ klasorune TASINIR.
    Agent yedek varsa geri yuklenir, yoksa Cuberite sifirdan baslar.

    Taranan konumlar:
      • players/**/*.json          — oyuncu kayit dosyalari (alt klasorler dahil)
      • world/data/stats/*.json    — istatistik dosyalari (bunlar bozulunca kick eder!)
    """
    import json as _j, shutil as _sh, subprocess as _spe

    players_dir = MC_DIR / "players"
    corrupt_dir = MC_DIR / "players_corrupted"
    backup_dir  = MC_DIR / "players_backup"
    stats_dir   = MC_DIR / "world" / "data" / "stats"

    for _d in [players_dir, corrupt_dir, backup_dir, stats_dir,
               MC_DIR / "world" / "playerdata"]:
        _d.mkdir(parents=True, exist_ok=True)
    try:
        _spe.run(["chmod", "-R", "777", str(players_dir)], capture_output=True)
        _spe.run(["chmod", "-R", "777", str(MC_DIR / "world" / "data")], capture_output=True)
    except Exception: pass

    agents = [a for a in _agents.values() if a.get("healthy")]
    moved  = 0

    def _is_valid_json(path):
        try:
            txt = path.read_text(encoding="utf-8", errors="replace").strip()
            if not txt:
                return False
            return isinstance(_j.loads(txt), dict)
        except Exception:
            return False

    # ── 1. players/**/*.json — alt klasörler dahil (Cuberite players/XX/UUID.json yazar) ──
    for pf in list(players_dir.rglob("*.json")):
        if not pf.is_file() or corrupt_dir in pf.parents:
            continue
        if _is_valid_json(pf):
            continue

        # Bozuk — yedekle
        try: _sh.copy2(str(pf), str(backup_dir / pf.name))
        except Exception: pass

        restored = False
        for _ag in agents:
            try:
                with _urllib_req.urlopen(
                        _ag["url"] + f"/api/files/players/{pf.name}", timeout=15) as _r:
                    _data = _r.read()
                if _data and len(_data) > 4:
                    pf.write_bytes(_data)
                    log(f"[Players] {pf.name} agent'tan geri yuklendi")
                    restored = True; break
            except Exception: pass

        if not restored:
            try:
                _sh.move(str(pf), str(corrupt_dir / pf.name))
                log(f"[Players] {pf.name} -> players_corrupted/ TASINDI (SILINMEDI)")
            except Exception as _e:
                log(f"[Players] {pf.name} tasinamadi: {_e}")
        moved += 1

    # ── 2. world/data/stats/*.json — bozuk istatistik → SİL (Cuberite "not found" ile graceful devam eder) ──
    # ⚠️  {} YAZMA! Cuberite stats JSON'unu stream ile okur; beklediği alanlar yoksa basic_ios::clear fırlatır.
    # Doğru davranış: bozuk dosyayı sil → Cuberite "not found, resetting to defaults" diyerek geçer.
    stats_fixed = 0
    for sf in list(stats_dir.glob("*.json")):
        if not sf.is_file():
            continue
        if _is_valid_json(sf):
            continue
        # Bozuk stats dosyası — yedekle ve SİL
        try:
            _sh.copy2(str(sf), str(corrupt_dir / ("stats_" + sf.name)))
        except Exception: pass
        try:
            sf.unlink()
            log(f"[Players] stats/{sf.name} bozuktu -> silindi (Cuberite yeniden olusturacak)")
            stats_fixed += 1
        except Exception as _e:
            log(f"[Players] stats/{sf.name} silinemedi: {_e}")

    n = len(list(players_dir.rglob("*.json")))
    if moved or stats_fixed:
        log(f"[Players] {moved} kayit + {stats_fixed} stats dosyasi islendi, {n} kayit kaldi")
    else:
        log(f"[Players] {n} oyuncu dosyasi saglikli" if n else
            "[Players] Kayitli oyuncu yok - ilk girislerinde olusturulur")

def write_server_config():
    """
    Cuberite INI konfigürasyon dosyaları yaz.
    Paper server.properties → settings.ini
    Paper paper-world-defaults.yml → world.ini
    """
    MC_DIR.mkdir(parents=True, exist_ok=True)

    # ── settings.ini — Ana sunucu ayarları ─────────────────────
    settings = MC_DIR / "settings.ini"
    settings.write_text(
        f"[Server]\n"
        f"Ports={MC_PORT}\n"
        f"MaxPlayers=20\n"
        f"OnlineMode=false\n"        # Render'da auth sunucusuna erişim yok
        f"Motd=\u00A7aRender MC • Cuberite 1.8.8\n"
        f"AllowFlight=true\n"
        f"Description=Cuberite 1.8.8 on Render\n"
        f"ShutdownMessage=Server kapaniyor...\n"
        f"\n"
        f"[Authentication]\n"
        f"Authenticate=false\n"      # Offline mode
        f"\n"
        f"[AntiCheat]\n"
        f"LimitPlayerBlockChanges=false\n"
        f"AllowFlight=true\n"
        f"\n"
        f"[Plugins]\n"
        f"Core=1\n"
        f"ChatLog=1\n"
        f"PlayerSave=1\n"            # stats/player dosyası iostream hatasını önler
    )

    # ── world.ini — Dünya ayarları ──────────────────────────────
    # Cuberite, world.ini'yi dünya klasörünün içinde arar:
    # /minecraft/world/world.ini  (NOT /minecraft/world.ini)
    (MC_DIR / "world").mkdir(parents=True, exist_ok=True)
    world_ini = MC_DIR / "world" / "world.ini"
    if not world_ini.exists():
        world_ini.write_text(
            f"[General]\n"
            f"Dimension=Overworld\n"
            f"WorldType=Normal\n"
            f"Seed=12345\n"
            f"Difficulty=1\n"
            f"Gamemode=0\n"
            f"PVPEnabled=1\n"
            f"AllowFlight=1\n"
            f"\n"
            f"[SpawnPosition]\n"
            f"X=0\n"
            f"Y=64\n"
            f"Z=0\n"
            f"\n"
            f"[Mobs]\n"
            f"MaxMobDistanceFromPlayer=80\n"
            f"MaxAnimals=4\n"
            f"MaxMonsters=20\n"
            f"MaxWaterMobs=2\n"
            f"\n"
            f"[Chunking]\n"
            f"ChunkDestroyTimer=30\n"
            f"LimitedHeightWorld=false\n"
            f"\n"
            f"[WorldLimit]\n"
            f"LimitX=1500\n"
            f"LimitZ=1500\n"
        )

    # world_nether/world.ini
    (MC_DIR / "world_nether").mkdir(parents=True, exist_ok=True)
    nether_ini = MC_DIR / "world_nether" / "world.ini"
    if not nether_ini.exists():
        nether_ini.write_text(
            "[General]\nDimension=Nether\nWorldType=Normal\nSeed=12345\n"
            "Difficulty=1\nGamemode=0\n"
        )

    # ── webadmin.ini — Cuberite web admin (kapalı, Panel var) ──
    webadmin = MC_DIR / "webadmin.ini"
    if not webadmin.exists():
        webadmin.write_text("[WebAdmin]\nEnabled=false\n")

    # ── Tüm gerekli dizinler — HEPSİ başlamadan önce oluşturulmalı ──────
    # iostream error = dizin yok veya yazma izni yok
    # Özellikle world/data/stats/ kritik — Cuberite oyuncu girişinde buraya yazar
    import subprocess as _sp
    _mkdirs = [
        MC_DIR / "players",
        MC_DIR / "players_backup",
        MC_DIR / "players_corrupted",
        MC_DIR / "world" / "data",
        MC_DIR / "world" / "data" / "stats",
        MC_DIR / "world" / "playerdata",
        MC_DIR / "world" / "region",
        MC_DIR / "world_nether" / "data",
        MC_DIR / "world_nether" / "data" / "stats",
        MC_DIR / "world_nether" / "DIM-1" / "region",
        MC_DIR / "world_the_end",
        MC_DIR / "world_the_end" / "data",
        MC_DIR / "world_the_end" / "data" / "stats",
        MC_DIR / "world_the_end" / "DIM1" / "region",
        MC_DIR / "logs",
        MC_DIR / "crash-reports",
    ]
    for _d in _mkdirs:
        _d.mkdir(parents=True, exist_ok=True)
    try:
        _sp.run(["chmod", "-R", "777", str(MC_DIR)], capture_output=True, timeout=15)
        log("[Panel] chmod 777 /minecraft OK")
    except Exception as _ce:
        log(f"[Panel] chmod uyarisi: {_ce}")

    # scoreboard.dat — Geçerli GZIP+NBT binary yaz
    # JSON yazmak "Data extraction failed" verir çünkü Cuberite binary NBT bekler
    def _write_empty_scoreboard(path):
        import struct, gzip as _gz, io as _io
        def _nbt_str(s):
            b = s.encode('utf-8')
            return struct.pack('>H', len(b)) + b
        def _nbt_list(name, etype, count):
            return bytes([9]) + _nbt_str(name) + bytes([etype]) + struct.pack('>i', count)
        def _nbt_compound(name, payload):
            return bytes([10]) + _nbt_str(name) + payload + bytes([0])
        data_payload = (
            _nbt_list('Objectives',   10, 0) +
            _nbt_list('PlayerScores', 10, 0) +
            _nbt_list('Teams',        10, 0) +
            _nbt_compound('DisplaySlots', b'')
        )
        root = bytes([10]) + _nbt_str('') + _nbt_compound('data', data_payload) + bytes([0])
        buf = _io.BytesIO()
        with _gz.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
            gz.write(root)
        path.write_bytes(buf.getvalue())

    for _world in ("world", "world_nether", "world_the_end"):
        _sbd = MC_DIR / _world / "data" / "scoreboard.dat"
        if not _sbd.exists():
            try:
                _write_empty_scoreboard(_sbd)
                log(f"[Panel] {_world}/data/scoreboard.dat oluşturuldu (boş NBT)")
            except Exception as _e:
                log(f"[Panel] scoreboard.dat yazılamadı ({_world}): {_e}")

    # OYUNCU DOSYALARI ASLA SILINMEZ — _clean_player_files() halleder



def get_cuberite_cmd() -> list:
    """
    Cuberite C++ baslatma komutu.
    Shell wrapper: baslamadan once chmod + dizinler + scoreboard sil.
    """
    wrapper = MC_DIR / "_start_cuberite.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"umask 000\n"
        # TÜM gerekli dizinleri önceden oluştur — Cuberite başlamadan hazır olsun
        f"mkdir -p"
        f" {MC_DIR}/players"
        f" {MC_DIR}/world/data/stats"
        f" {MC_DIR}/world/data"
        f" {MC_DIR}/world/playerdata"
        f" {MC_DIR}/world/players"
        f" {MC_DIR}/world_nether/data/stats"
        f" {MC_DIR}/world_the_end/data/stats"
        f" {MC_DIR}/logs"
        f"\n"
        # İzin ver
        f"chmod -R 777 {MC_DIR}\n"
        # scoreboard.dat — sadece yoksa oluştur (NBT binary _prepare_dirs'da yazılır)
        f"[ -f {MC_DIR}/world/data/scoreboard.dat ] || true\n"
        # Çalışma dizinine geç
        f"cd {MC_DIR}\n"
        # Başlat
        f"exec {MC_BIN} --config-file {MC_DIR}/settings.ini\n"
    )
    wrapper.chmod(0o755)
    log("[Panel] Cuberite C++ baslatiliyor (JVM yok, ~50MB RAM)")
    return ["/bin/sh", str(wrapper)]


def get_jvm_args():
    """Geriye dönük uyumluluk — Cuberite C++ kullanıyor, çağrılmaz."""
    return get_cuberite_cmd()


def _worker_proxy(path: str, body: dict = None) -> dict:
    """WORKER_URL (ana sunucu MC_ONLY API) üzerindeki endpoint'i çağır."""
    try:
        req = _urllib_req.Request(
            WORKER_URL + path,
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _urllib_req.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def start_server():
    global mc_process
    # Panel-agent modu: komutu ana sunucuya yönlendir
    if IS_PROXY:
        r = _worker_proxy("/api/mc/start")
        if r.get("ok"):
            server_state["status"] = "starting"
            socketio.emit("server_status", server_state)
        return r.get("ok", False), r.get("msg", "Proxy çağrısı")
    if mc_process and mc_process.poll() is None:
        return False, "Server zaten çalışıyor"
    MC_DIR.mkdir(parents=True, exist_ok=True)
    if not MC_BIN.exists():
        server_state["status"] = "downloading"
        socketio.emit("server_status", server_state)
        if not download_cuberite():
            server_state["status"] = "stopped"
            socketio.emit("server_status", server_state)
            return False, "Cuberite indirilemedi"
    write_server_config()

    # ── Her başlatmada PROAKTİF hazırlık — Cuberite başlamadan önce ──────────
    # REACTIVE değil — hata GELDIKTEN SONRA değil, BAŞLAMADAN ÖNCE çalıştır
    _ensure_runtime_dirs()   # world/data/stats/ + diğer kritik dizinler
    _patch_cuberite_data()   # Protocol placeholders + JungleTemple BambooJungle fix
    _clean_player_files()    # Bozuk .json → corrupted/ (stats dahil)

    server_state.update({"status": "starting", "online_players": 0})
    players.clear()
    socketio.emit("server_status", server_state)
    socketio.emit("players_update", [])
    # Offload edilmiş region'ları agent'tan geri yükle
    threading.Thread(target=_region_restore_all, daemon=True).start()
    time.sleep(2)  # Küçük bekleme — kritik region'lar için

    cmd = get_cuberite_cmd()
    log(f"[Panel] 🚀 Cuberite C++ başlatılıyor (JVM yok)...")
    try:
        def _child_setup():
            try:
                import resource as _r
                _r.setrlimit(_r.RLIMIT_AS, (_r.RLIM_INFINITY, _r.RLIM_INFINITY))
            except Exception: pass
            try:
                open("/proc/self/oom_score_adj", "w").write("-900")
            except Exception: pass
        mc_process = subprocess.Popen(
            cmd, cwd=str(MC_DIR),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            preexec_fn=_child_setup,
        )
    except Exception as e:
        log(f"[Panel] ❌ Başlatma hatası: {e}")
        server_state["status"] = "stopped"
        socketio.emit("server_status", server_state)
        return False, str(e)
    threading.Thread(target=_stdout_reader, daemon=True).start()
    log(f"[Panel] ✅ Cuberite PID={mc_process.pid} (~50MB RAM, JVM yok)")
    return True, "Başlatılıyor..."


def stop_server(force=False):
    global mc_process
    if IS_PROXY:
        r = _worker_proxy("/api/mc/stop", {"force": force})
        return r.get("ok", False), r.get("msg", "")
    if not mc_process or mc_process.poll() is not None:
        return False, "Server çalışmıyor"
    server_state["status"] = "stopping"
    socketio.emit("server_status", server_state)
    if force:
        mc_process.kill()
    else:
        # Cuberite konsol komutu: "stop" (save komutu yok — otomatik kaydeder)
        send_command("stop"); time.sleep(3)
    try: _backup_players_to_agents()
    except Exception: pass
    return True, "Durduruluyor..."


def send_command(cmd: str) -> bool:
    if IS_PROXY:
        return _worker_proxy("/api/mc/command", {"cmd": cmd}).get("ok", False)
    if mc_process and mc_process.poll() is None:
        try:
            mc_process.stdin.write(f"{cmd}\n".encode())
            mc_process.stdin.flush()
            return True
        except: pass
    return False


def _ram_watchdog():
    """
    Bellek baskısını sürekli izle.
    512MB fiziksel limit aşılmadan swap'a geçilmesini sağla.
    """
    import psutil
    _consecutive_low = 0
    while True:
        eventlet.sleep(4)
        try:
            mem  = psutil.virtual_memory()
            swp  = psutil.swap_memory()
            # Gerçek kullanılabilir = fiziksel boş + swap boş
            phys_avail_mb = mem.available  // 1024 // 1024
            swap_free_mb  = swp.free       // 1024 // 1024

            # Fiziksel RAM kritik eşikte (<80MB) — agresif temizlik
            if phys_avail_mb < 80:
                _consecutive_low += 1
                # Kernel sayfa cache'ini boşalt
                try: open("/proc/sys/vm/drop_caches","w").write("1")
                except: pass
                # swappiness'i maksimuma çek (kernel swap'ı tercih etsin)
                try: open("/proc/sys/vm/swappiness","w").write("200")
                except: pass

                if _consecutive_low >= 3:
                    # Cuberite'de item/xp entity temizleme Lua API ile yapılır.
                    # Konsol komutu yok — sadece save ile chunk flush et
                    send_command("save")
                if _consecutive_low >= 6:
                    send_command("save")
                log(f"[Panel] ⚠️  RAM kritik: phys={phys_avail_mb}MB swap_free={swap_free_mb}MB")

            elif phys_avail_mb < 150:
                _consecutive_low = max(0, _consecutive_low - 1)
                try: open("/proc/sys/vm/drop_caches","w").write("1")
                except: pass

            else:
                _consecutive_low = 0
                # Cuberite'de runtime view-distance değişikliği yok — no-op

        except: pass


# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES — MC Yönetimi
# ══════════════════════════════════════════════════════════════

@app.route("/api/start", methods=["POST"])
def api_start():
    ok, msg = start_server()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = stop_server((request.json or {}).get("force", False))
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    stop_server(); time.sleep(4)
    ok, msg = start_server()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/status")
def api_status():
    return jsonify({**server_state, "players": _players_list(),
                    "tunnel": tunnel_info,
                    "pool": vcluster.summary()})


@app.route("/api/command", methods=["POST"])
def api_command():
    cmd = (request.json or {}).get("cmd", "").strip()
    return jsonify({"ok": send_command(cmd) if cmd else False})


@app.route("/api/console/history")
def api_console_history():
    return jsonify(list(console_buf))


@app.route("/api/internal/tunnel", methods=["POST"])
def api_internal_tunnel():
    d = request.json or {}
    tunnel_info.update({"url": d.get("url",""), "host": d.get("host","")})
    socketio.emit("tunnel_update", tunnel_info)
    return jsonify({"ok": True})


@app.route("/api/internal/status_msg", methods=["POST"])
def api_internal_status_msg():
    msg = (request.json or {}).get("msg", "")
    if msg: log(msg)
    return jsonify({"ok": True})


@app.route("/api/tunnel")
def api_tunnel():
    return jsonify(tunnel_info)


# ── Eski destek düğümü uyumluluğu (v9.x agent'lar için) ──────

@app.route("/api/worker/register", methods=["POST"])
def api_worker_register():
    d = request.json or {}
    host = d.get("worker_host", "")
    if host:
        log(f"[Panel] ⚙️  Eski destek düğümü: {host} (v9.x uyumlu)")
    return jsonify({"ok": True})


@app.route("/api/worker/heartbeat", methods=["POST"])
def api_worker_heartbeat():
    return jsonify({"ok": True})


@app.route("/api/worker/status")
def api_worker_status():
    return jsonify({"nodes": []})


# ══════════════════════════════════════════════════════════════
#  RESOURCE POOL — Agent API (v10.0)
# ══════════════════════════════════════════════════════════════

@app.route("/api/agent/register", methods=["POST"])
def api_agent_register():
    d          = request.json or {}
    tunnel_url = d.get("tunnel", "")
    node_id    = d.get("node_id", "")
    if not tunnel_url or not node_id:
        return jsonify({"ok": False, "error": "tunnel veya node_id eksik"})
    # İkisi de güncelle: eski _pool + vcluster
    _pool_register(tunnel_url, node_id, d)
    vcluster.register_agent(tunnel_url, node_id, d)
    log(f"[Pool] ✅ Agent kayıt: {node_id} | "
        f"RAM free:{d.get('ram',{}).get('free_mb',0)}MB | "
        f"Disk free:{d.get('disk',{}).get('store_free_gb',d.get('disk',{}).get('free_gb',0)):.1f}GB")
    socketio.emit("pool_update", vcluster.summary())
    return jsonify({"ok": True})


@app.route("/api/agent/heartbeat", methods=["POST"])
def api_agent_heartbeat():
    d      = request.json or {}
    nid    = d.get("node_id", "")
    tunnel = d.get("tunnel", "")
    if not nid:
        return jsonify({"ok": True})
    if tunnel:
        # Tunnel URL var → tam kayıt / güncelle
        _pool_register(tunnel, nid, d)
        vcluster.register_agent(tunnel, nid, d)
    else:
        # Tunnel yok (Render EXTERNAL_URL henüz set olmamış) → sadece info güncelle
        with vcluster._lock:
            if nid in vcluster._agents:
                vcluster._agents[nid]["info"]       = d
                vcluster._agents[nid]["last_ping"]  = __import__("time").time()
                vcluster._agents[nid]["healthy"]    = True
                vcluster._agents[nid]["fail_count"] = 0
    socketio.emit("pool_update", vcluster.summary())
    return jsonify({"ok": True})


@app.route("/api/pool/status")
def api_pool_status():
    return jsonify(vcluster.summary())


@app.route("/api/pool/cache/stats")
def api_pool_cache_stats():
    stats = []
    with vcluster._lock:
        agents = list(vcluster._agents.values())
    for ag in agents:
        r = _agent_json(ag, "GET", "/api/cache/stats")
        if r:
            r["node_id"] = ag["node_id"]
            stats.append(r)
    return jsonify({
        "agents":     stats,
        "total_keys": sum(s.get("keys",   0) for s in stats),
        "total_mb":   sum(s.get("used_mb",0) for s in stats),
    })


@app.route("/api/pool/cache/flush", methods=["POST"])
def api_pool_cache_flush():
    prefix = (request.json or {}).get("prefix", "")
    total  = 0
    with vcluster._lock:
        agents = list(vcluster._agents.values())
    for ag in agents:
        r = _agent_json(ag, "POST", "/api/cache/flush", {"prefix": prefix})
        total += (r or {}).get("flushed", 0)
    log(f"[Pool] 🗑️  {total} önbellek anahtarı temizlendi")
    return jsonify({"ok": True, "flushed": total})


@app.route("/api/pool/storage")
def api_pool_storage():
    result = []
    with vcluster._lock:
        agents = list(vcluster._agents.values())
    for ag in agents:
        r = _agent_json(ag, "GET", "/api/files/storage/stats")
        if r:
            r["node_id"] = ag["node_id"]
            result.append(r)
    return jsonify({"agents": result})


@app.route("/api/pool/task", methods=["POST"])
def api_pool_task():
    d = request.json or {}
    with _agents_lock:
        agents = [a for a in _agents.values() if a["healthy"]]
    if not agents:
        return jsonify({"ok": False, "error": "Aktif agent yok"})
    import hashlib as _hlib
    key = d.get("type","") + str(d.get("payload",""))
    ag  = agents[int(_hlib.md5(key.encode()).hexdigest(),16) % len(agents)]
    r   = _agent_json(ag, "POST", "/api/cpu/submit", d)
    if not r:
        return jsonify({"ok": False, "error": "Görev gönderilemedi"})
    task_id = r.get("task_id")
    for _ in range(30):
        time.sleep(1)
        res = _agent_json(ag, "GET", f"/api/cpu/result/{task_id}")
        if res and res.get("status") == "done":
            return jsonify({"ok": True, "result": res.get("result")})
    return jsonify({"ok": True, "task_id": task_id, "status": "pending"})


@app.route("/api/pool/proxy/start", methods=["POST"])
def api_pool_proxy_start():
    d    = request.json or {}
    host = d.get("host", "127.0.0.1")
    port = int(d.get("port", 25565))
    with vcluster._lock:
        agents = list(vcluster._agents.values())
    started = []
    for ag in agents:
        r = _agent_json(ag, "POST", "/api/proxy/start",
                        {"host": host, "port": port, "listen_port": 25565})
        if r and r.get("ok"):
            started.append(ag["node_id"])
            log(f"[Pool] 🔀 Proxy: {ag['node_id']} → {host}:{port}")
    return jsonify({"ok": True, "started": started})


@app.route("/api/pool/proxy/stop", methods=["POST"])
def api_pool_proxy_stop():
    with vcluster._lock:
        agents = list(vcluster._agents.values())
    for ag in agents:
        _agent_json(ag, "POST", "/api/proxy/stop")
    return jsonify({"ok": True})


@app.route("/api/pool/sync/status")
def api_pool_sync_status():
    with _region_lock:
        offloaded = list(_offloaded)
        synced    = list(_synced)
    agents = [a for a in _agents.values() if a["healthy"]]
    total_disk = sum(a["info"].get("disk", {}).get("store_used_gb", 0) for a in agents)
    total_ram  = sum(a["info"].get("ram",  {}).get("cache_mb", 0)      for a in agents)
    return jsonify({
        "ok": True,
        "offloaded":          len(offloaded),
        "synced":             len(synced),
        "agent_disk_used_gb": round(total_disk, 2),
        "agent_ram_cache_mb": total_ram,
    })


@app.route("/api/pool/restore", methods=["POST"])
def api_pool_restore():
    threading.Thread(target=_region_restore_all, daemon=True).start()
    return jsonify({"ok": True, "msg": "Geri yükleme başlatıldı"})


@app.route("/api/pool/archive/regions", methods=["POST"])
def api_pool_archive_regions():
    result = _auto_archive_old_regions(
        older_than_days=int((request.json or {}).get("older_than_days", 3))
    )
    if result:
        archived, freed_mb = result
        return jsonify({"ok": True, "archived": archived, "freed_mb": round(freed_mb,1)})
    return jsonify({"ok": False, "error": "Agent yok veya region bulunamadı"})


# ── Oyuncu yönetimi ───────────────────────────────────────────

@app.route("/api/players")
def api_players():
    send_command("list")
    return jsonify({"players": _players_list(), "count": len(players)})

@app.route("/api/players/kick",     methods=["POST"])
def api_kick():
    d=request.json or {}; send_command(f"kick {d['player']} {d.get('reason','Kicked')}"); return jsonify({"ok":True})
@app.route("/api/players/ban",      methods=["POST"])
def api_ban():
    d=request.json or {}; send_command(f"ban {d['player']} {d.get('reason','Banned')}"); return jsonify({"ok":True})
@app.route("/api/players/ban-ip",   methods=["POST"])
def api_ban_ip():
    send_command(f"ban-ip {(request.json or {})['player']}"); return jsonify({"ok":True})
@app.route("/api/players/pardon",   methods=["POST"])
def api_pardon():
    send_command(f"pardon {(request.json or {})['player']}"); return jsonify({"ok":True})
@app.route("/api/players/op",       methods=["POST"])
def api_op():
    send_command(f"op {(request.json or {})['player']}"); return jsonify({"ok":True})
@app.route("/api/players/deop",     methods=["POST"])
def api_deop():
    send_command(f"deop {(request.json or {})['player']}"); return jsonify({"ok":True})
@app.route("/api/players/gamemode", methods=["POST"])
def api_gamemode():
    d=request.json or {}; send_command(f"gamemode {d['mode']} {d['player']}"); return jsonify({"ok":True})
@app.route("/api/players/tp",       methods=["POST"])
def api_tp():
    d=request.json or {}; send_command(f"tp {d['player']} {d.get('to') or d.get('player')}"); return jsonify({"ok":True})
@app.route("/api/players/give",     methods=["POST"])
def api_give():
    d=request.json or {}; send_command(f"give {d['player']} {d['item']} {d.get('count',1)}"); return jsonify({"ok":True})
@app.route("/api/players/msg",      methods=["POST"])
def api_msg():
    d=request.json or {}; send_command(f"tell {d['player']} {d['message']}"); return jsonify({"ok":True})
@app.route("/api/players/heal",     methods=["POST"])
def api_heal():
    p=(request.json or {}).get("player","@a")
    send_command(f"effect give {p} regeneration 5 255 true")
    send_command(f"effect give {p} saturation 5 255 true")
    return jsonify({"ok":True})
@app.route("/api/players/kill",     methods=["POST"])
def api_kill_player():
    send_command(f"kill {(request.json or {}).get('player','')}"); return jsonify({"ok":True})

@app.route("/api/banlist")
def api_banlist():
    f=MC_DIR/"banned-players.json"; return jsonify(json.loads(f.read_text()) if f.exists() else [])
@app.route("/api/whitelist")
def api_whitelist():
    f=MC_DIR/"whitelist.json"; return jsonify(json.loads(f.read_text()) if f.exists() else [])
@app.route("/api/whitelist/add",    methods=["POST"])
def api_wl_add():
    send_command(f"whitelist add {(request.json or {})['player']}"); return jsonify({"ok":True})
@app.route("/api/whitelist/remove", methods=["POST"])
def api_wl_rm():
    send_command(f"whitelist remove {(request.json or {})['player']}"); return jsonify({"ok":True})
@app.route("/api/whitelist/toggle", methods=["POST"])
def api_wl_toggle():
    send_command("whitelist on" if (request.json or {}).get("on",True) else "whitelist off"); return jsonify({"ok":True})

# ── Dosya yönetimi ────────────────────────────────────────────

def safe_path(rel):
    p=(MC_DIR/rel).resolve()
    if not str(p).startswith(str(MC_DIR.resolve())): abort(403)
    return p

@app.route("/api/files")
def api_files():
    p=safe_path(request.args.get("path",""))
    if not p.exists(): return jsonify([])
    items=[]
    for item in sorted(p.iterdir(),key=lambda x:(x.is_file(),x.name.lower())):
        stat=item.stat()
        size=sum(f.stat().st_size for f in item.rglob("*") if f.is_file()) if item.is_dir() else stat.st_size
        items.append({"name":item.name,"path":str(item.relative_to(MC_DIR)),"type":"dir" if item.is_dir() else "file","size":size,"modified":int(stat.st_mtime),"ext":item.suffix.lower() if item.is_file() else ""})
    return jsonify(items)

@app.route("/api/files/read")
def api_file_read():
    p=safe_path(request.args.get("path",""))
    if not p.is_file(): abort(404)
    try: return jsonify({"content":p.read_text(errors="replace"),"path":str(p.relative_to(MC_DIR))})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/files/write", methods=["POST"])
def api_file_write():
    d=request.json or {}; p=safe_path(d["path"]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(d["content"]); return jsonify({"ok":True})

@app.route("/api/files/delete", methods=["POST"])
def api_file_delete():
    p=safe_path((request.json or {})["path"])
    shutil.rmtree(p) if p.is_dir() else p.unlink(); return jsonify({"ok":True})

@app.route("/api/files/mkdir",  methods=["POST"])
def api_mkdir():
    safe_path((request.json or {})["path"]).mkdir(parents=True,exist_ok=True); return jsonify({"ok":True})

@app.route("/api/files/rename", methods=["POST"])
def api_rename():
    d=request.json or {}; safe_path(d["from"]).rename(safe_path(d["to"])); return jsonify({"ok":True})

@app.route("/api/files/upload", methods=["POST"])
def api_upload():
    rel=request.form.get("path","")
    for _,f in request.files.items():
        p=safe_path(rel+"/"+f.filename); p.parent.mkdir(parents=True,exist_ok=True); f.save(str(p))
    return jsonify({"ok":True})

@app.route("/api/files/download")
def api_download():
    p=safe_path(request.args.get("path",""))
    if not p.exists(): abort(404)
    if p.is_dir():
        zp=f"/tmp/{p.name}.zip"
        with zipfile.ZipFile(zp,"w",zipfile.ZIP_DEFLATED) as z:
            for fp in p.rglob("*"):
                if fp.is_file(): z.write(fp,fp.relative_to(p))
        return send_file(zp,as_attachment=True,download_name=f"{p.name}.zip")
    return send_file(str(p),as_attachment=True)

# ── Plugin yönetimi ───────────────────────────────────────────

@app.route("/api/plugins")
def api_plugins():
    pdir=MC_DIR/"plugins"; pdir.mkdir(exist_ok=True)
    result=[]
    for jar in sorted(pdir.glob("*.jar")):
        result.append({"name":jar.stem,"file":jar.name,"size":jar.stat().st_size,"enabled":True})
    for jar in sorted(pdir.glob("*.jar.disabled")):
        result.append({"name":jar.name.replace(".jar.disabled",""),"file":jar.name,"size":jar.stat().st_size,"enabled":False})
    return jsonify(result)

@app.route("/api/plugins/upload", methods=["POST"])
def api_plugin_upload():
    pdir=MC_DIR/"plugins"; pdir.mkdir(exist_ok=True); uploaded=[]
    for f in request.files.values():
        if f.filename.endswith(".jar"):
            f.save(str(pdir/f.filename)); uploaded.append(f.filename)
    return jsonify({"ok":True,"uploaded":uploaded,"msg":f"{len(uploaded)} plugin yüklendi."})

@app.route("/api/plugins/delete", methods=["POST"])
def api_plugin_delete():
    p=MC_DIR/"plugins"/(request.json or {})["file"]
    if p.exists(): p.unlink()
    return jsonify({"ok":True})

@app.route("/api/plugins/toggle", methods=["POST"])
def api_plugin_toggle():
    name=(request.json or {})["file"]; p=MC_DIR/"plugins"/name
    new=MC_DIR/"plugins"/(name[:-len(".disabled")] if name.endswith(".disabled") else name+".disabled")
    if p.exists(): p.rename(new)
    return jsonify({"ok":True})

@app.route("/api/plugins/search")
def api_plugin_search():
    q=request.args.get("q","")
    if not q: return jsonify([])
    try:
        ctx=_ssl_mod.create_default_context()
        url="https://hangar.papermc.io/api/v1/projects?"+_urllib_parse.urlencode({"q":q,"limit":12})
        with _urllib_req.urlopen(_urllib_req.Request(url,headers={"User-Agent":"MCPanel/10.0"}),timeout=10,context=ctx) as resp:
            data=json.loads(resp.read())
        out=[]
        for p in data.get("result",[]):
            ns=p.get("namespace",{}); owner=ns.get("owner",""); name=p.get("name","")
            out.append({"name":name,"description":p.get("description","")[:120],"downloads":p.get("stats",{}).get("downloads",0),"url":f"https://hangar.papermc.io/{owner}/{name}","owner":owner})
        return jsonify(out)
    except Exception as e:
        return jsonify({"error":str(e)}),500

# ── Ayarlar ───────────────────────────────────────────────────

@app.route("/api/settings")
def api_settings():
    """Cuberite settings.ini okur — INI formatı (section başlıkları atlanır)."""
    f = MC_DIR / "settings.ini"
    if not f.exists():
        # settings.ini yoksa write_server_config() çalıştır, sonra oku
        write_server_config()
    props = {}
    try:
        for line in f.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("[") or line.startswith(";"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
    except Exception as e:
        return jsonify({"error": str(e)})
    return jsonify(props)


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """Cuberite settings.ini günceller (sadece [Server] ve [Authentication] bölümleri)."""
    updates = request.json or {}
    f = MC_DIR / "settings.ini"

    # Mevcut içeriği oku
    sections: dict[str, list] = {}   # {section_name: [lines]}
    cur_section = "__top__"
    sections[cur_section] = []
    try:
        for line in f.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                cur_section = stripped[1:-1]
                sections.setdefault(cur_section, [])
            else:
                sections.setdefault(cur_section, []).append(line)
    except Exception:
        # Yoksa varsayılan oluştur
        write_server_config()
        return jsonify({"ok": True, "msg": "Varsayılan config oluşturuldu. Yeniden başlatın."})

    # Değerleri güncelle
    for upd_key, upd_val in updates.items():
        placed = False
        for sec_name, sec_lines in sections.items():
            for i, ln in enumerate(sec_lines):
                if "=" in ln:
                    k = ln.split("=", 1)[0].strip()
                    if k == upd_key:
                        sec_lines[i] = f"{k}={upd_val}"
                        placed = True
                        break
            if placed:
                break
        if not placed:
            # [Server] bölümüne ekle
            sections.setdefault("Server", []).append(f"{upd_key}={upd_val}")

    # Geri yaz
    out_lines = []
    for sec_name, sec_lines in sections.items():
        if sec_name != "__top__":
            out_lines.append(f"[{sec_name}]")
        out_lines.extend(sec_lines)
    f.write_text("\n".join(out_lines) + "\n")
    return jsonify({"ok": True, "msg": "settings.ini kaydedildi. Yeniden başlatın."})

# ── Dünya / Yedek ─────────────────────────────────────────────

@app.route("/api/worlds")
def api_worlds():
    worlds=[]
    for d in MC_DIR.iterdir():
        if d.is_dir() and (d/"level.dat").exists():
            size=sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            worlds.append({"name":d.name,"size":size,"modified":int(d.stat().st_mtime)})
    return jsonify(worlds)

@app.route("/api/worlds/backup", methods=["POST"])
def api_world_backup():
    world=(request.json or {}).get("world","world"); src=MC_DIR/world
    if not src.exists(): return jsonify({"ok":False,"error":"Dünya bulunamadı"})
    ts=datetime.now().strftime("%Y%m%d_%H%M%S"); dest=MC_DIR/"backups"/f"{world}_{ts}.zip"
    dest.parent.mkdir(exist_ok=True)
    send_command("save"); time.sleep(2)   # Cuberite: save-off/save-all/save-on yok → sadece save
    with zipfile.ZipFile(str(dest),"w",zipfile.ZIP_DEFLATED) as z:
        for fp in src.rglob("*"):
            if fp.is_file(): z.write(fp,fp.relative_to(MC_DIR))
    # save-on yok — save sonrası otomatik devam eder
    return jsonify({"ok":True,"file":str(dest.relative_to(MC_DIR)),"size":dest.stat().st_size})

@app.route("/api/worlds/delete", methods=["POST"])
def api_world_delete():
    world=(request.json or {}).get("world"); p=MC_DIR/world if world else None
    if p and p.exists() and (p/"level.dat").exists():
        shutil.rmtree(p); return jsonify({"ok":True})
    return jsonify({"ok":False,"error":"Dünya bulunamadı"})

@app.route("/api/backups")
def api_backups():
    bdir=MC_DIR/"backups"; bdir.mkdir(exist_ok=True)
    return jsonify([{"name":f.name,"path":str(f.relative_to(MC_DIR)),"size":f.stat().st_size,"created":int(f.stat().st_mtime)}
                    for f in sorted(bdir.glob("*.zip"),key=lambda x:x.stat().st_mtime,reverse=True)])

# ── Performans ────────────────────────────────────────────────

@app.route("/api/performance")
def api_performance():
    try:
        import psutil
        cpu=psutil.cpu_percent(0.2); vm=psutil.virtual_memory(); swp=psutil.swap_memory(); dk=psutil.disk_usage("/")
        procs=[]
        if mc_process and mc_process.poll() is None:
            try:
                proc=psutil.Process(mc_process.pid)
                procs=[{"cpu":round(proc.cpu_percent(),1),"ram":int(proc.memory_info().rss/1024/1024),"threads":proc.num_threads()}]
            except: pass
        pool     = vcluster.summary()
        vm_info  = pool.get("virtual_machine", {})
        mem_info = pool.get("memory", {})
        disk_info= pool.get("disk", {})
        # UserSwap stats (userswap.so JSON dosyasından)
        us = {}
        try:
            us = json.loads(open("/tmp/userswap.stats").read())
        except Exception:
            pass
        swap_total = us.get("total_mb", int(swp.total / 1024 / 1024))
        swap_free  = us.get("free_mb",  int(swp.free  / 1024 / 1024))
        return jsonify({
            "cpu": round(cpu, 1), "ram_pct": round(vm.percent, 1),
            "ram_used_mb": int(vm.used/1024/1024), "ram_total_mb": int(vm.total/1024/1024),
            "ram_free_mb": int(vm.available/1024/1024),
            "swap_total_mb": swap_total, "swap_free_mb": swap_free,
            "disk_pct": round(dk.percent, 1), "disk_used_gb": round(dk.used/1e9, 1),
            "disk_total_gb": round(dk.total/1e9, 1),
            "cpu_count": psutil.cpu_count(), "mc": procs[0] if procs else {},
            "tps": server_state["tps"], "tps5": server_state["tps5"],
            "tps15": server_state["tps15"],
            "pool_agents":   pool.get("healthy", 0),
            "pool_ram_mb":   vm_info.get("agent_cache_mb", 0),
            "pool_disk_gb":  disk_info.get("remote_gb", 0),
            "pool_cache_mb": mem_info.get("local_cache_mb", 0),
            "userswap":      us,
        })
    except Exception as e:
        return jsonify({"error":str(e)})


# ── SocketIO ──────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("console_history", list(console_buf))
    emit("server_status",   server_state)
    emit("players_update",  _players_list())
    emit("tunnel_update",   tunnel_info)
    emit("pool_update",     vcluster.summary())


@socketio.on("send_command")
def on_send_command(data):
    cmd=(data or {}).get("cmd","").strip()
    if cmd:
        ok=send_command(cmd)
        if not ok:
            emit("console_line",{"ts":datetime.now().strftime("%H:%M:%S"),
                                  "line":"[Panel] ⚠️  Server çalışmıyor"})


# ══════════════════════════════════════════════════════════════
#  PANEL HTML  (v10.0 — Kaynak Havuzu sekmesi eklendi)
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return PANEL_HTML


PANEL_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⛏️ MC Panel v10</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#05060c;--s1:#0b0d16;--s2:#0f1120;--s3:#131627;--a1:#00e5ff;--a2:#7c6aff;--a3:#00ffaa;--a4:#ff6b35;--red:#ff4757;--green:#2ed573;--yellow:#ffa502;--t1:#eef0f8;--t2:#8892a4;--t3:#3d4558;--font:'Sora',sans-serif;--mono:'JetBrains Mono',monospace;--sidebar:230px;--r:12px}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--t1);font-family:var(--font);overflow:hidden}
.layout{display:flex;height:100vh}
.sidebar{width:var(--sidebar);background:var(--s1);border-right:1px solid rgba(255,255,255,.06);display:flex;flex-direction:column;flex-shrink:0;overflow-y:auto}
.sb-head{padding:18px 16px 14px;border-bottom:1px solid rgba(255,255,255,.06)}
.sb-head h2{font-size:15px;font-weight:700;display:flex;align-items:center;gap:8px}
.sb-ver{font-size:10px;color:var(--t2);font-family:var(--mono);margin-top:4px}
.sb-status{margin:10px 10px 0;background:rgba(255,255,255,.03);border-radius:9px;padding:9px 12px;display:flex;align-items:center;gap:8px;font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot-green{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot-red{background:var(--red);box-shadow:0 0 6px var(--red)}
.dot-yellow{background:var(--yellow);box-shadow:0 0 6px var(--yellow);animation:blink 1s infinite}
.dot-purple{background:var(--a2);box-shadow:0 0 6px var(--a2)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.nav{padding:10px;flex:1}
.nav-sec{font-size:9px;font-weight:700;color:var(--t3);text-transform:uppercase;letter-spacing:.12em;padding:10px 6px 5px}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:9px;cursor:pointer;transition:all .15s;font-size:13px;color:var(--t2);margin-bottom:1px}
.nav-item:hover{background:rgba(255,255,255,.05);color:var(--t1)}
.nav-item.active{background:rgba(0,229,255,.09);color:var(--a1);font-weight:600}
.nav-item .ico{font-size:15px;width:18px;text-align:center}
.sb-ctrl{padding:12px 10px;border-top:1px solid rgba(255,255,255,.06)}
.ctrl-btn{width:100%;padding:8px;border-radius:9px;font-size:12px;font-weight:600;border:none;cursor:pointer;font-family:var(--font);transition:all .15s;margin-bottom:6px;display:flex;align-items:center;justify-content:center;gap:6px}
.cb-start{background:linear-gradient(135deg,#2ed573,#00a550);color:#000}
.cb-restart{background:rgba(255,165,2,.12);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.cb-stop{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.25)}
.ctrl-btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.topbar{height:50px;background:var(--s1);border-bottom:1px solid rgba(255,255,255,.06);display:flex;align-items:center;padding:0 18px;gap:14px;flex-shrink:0}
.page-title{font-size:14px;font-weight:700;flex:1}
.top-stats{display:flex;gap:18px;font-size:11px;color:var(--t2);font-family:var(--mono)}
.ts{display:flex;align-items:center;gap:4px}
.ts-v{color:var(--t1);font-weight:600}
.mc-addr-bar{background:linear-gradient(90deg,rgba(0,255,170,.08),rgba(0,229,255,.06));border-bottom:1px solid rgba(0,255,170,.15);padding:7px 18px;display:flex;align-items:center;gap:10px;font-size:12px;flex-shrink:0}
.mc-addr-bar.hidden{display:none}
.pool-bar{background:linear-gradient(90deg,rgba(124,106,255,.1),rgba(0,229,255,.06));border-bottom:1px solid rgba(124,106,255,.2);padding:6px 18px;display:flex;align-items:center;gap:12px;font-size:11px;flex-shrink:0}
.pool-bar.hidden{display:none}
.pages{flex:1;overflow:hidden}
.page{display:none;height:100%;overflow-y:auto;padding:18px}
.page.active{display:block}
.card{background:var(--s1);border:1px solid rgba(255,255,255,.06);border-radius:var(--r);padding:18px;margin-bottom:14px}
.card-hd{font-size:11px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.1em;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.card-hd::before{content:'';width:3px;height:11px;border-radius:2px;background:var(--a1)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.sc{background:var(--s2);border:1px solid rgba(255,255,255,.05);border-radius:10px;padding:16px;text-align:center}
.sc-val{font-size:26px;font-weight:700;font-family:var(--mono);background:linear-gradient(135deg,var(--a1),var(--a2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sc-lbl{font-size:11px;color:var(--t2);margin-top:4px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{padding:8px 10px;text-align:left;font-size:10px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid rgba(255,255,255,.06)}
.tbl td{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.04)}
.tbl tr:hover td{background:rgba(255,255,255,.02)}
.tbl tr:last-child td{border:none}
.badge{border-radius:20px;padding:2px 8px;font-size:10px;font-weight:700;font-family:var(--mono)}
.bg{background:rgba(46,213,115,.1);border:1px solid rgba(46,213,115,.25);color:var(--green)}
.br{background:rgba(255,71,87,.1);border:1px solid rgba(255,71,87,.25);color:var(--red)}
.bb{background:rgba(0,229,255,.08);border:1px solid rgba(0,229,255,.2);color:var(--a1)}
.by{background:rgba(255,165,2,.1);border:1px solid rgba(255,165,2,.25);color:var(--yellow)}
.bp{background:rgba(124,106,255,.1);border:1px solid rgba(124,106,255,.25);color:var(--a2)}
.btn{padding:6px 14px;border-radius:7px;font-size:12px;font-weight:600;border:none;cursor:pointer;font-family:var(--font);transition:all .15s;display:inline-flex;align-items:center;gap:5px;text-decoration:none;white-space:nowrap}
.btn:hover{transform:translateY(-1px)}
.btn-sm{padding:4px 10px;font-size:11px}
.btn-lg{padding:10px 22px;font-size:13px}
.b-prim{background:linear-gradient(135deg,var(--a1),var(--a2));color:#000}
.b-dang{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.25)}
.b-warn{background:rgba(255,165,2,.1);color:var(--yellow);border:1px solid rgba(255,165,2,.25)}
.b-succ{background:rgba(46,213,115,.1);color:var(--green);border:1px solid rgba(46,213,115,.25)}
.b-ghost{background:rgba(255,255,255,.05);color:var(--t2);border:1px solid rgba(255,255,255,.1)}
.b-purp{background:rgba(124,106,255,.12);color:var(--a2);border:1px solid rgba(124,106,255,.25)}
.con-wrap{height:calc(100% - 42px);display:flex;flex-direction:column}
.con-out{flex:1;background:#000;border-radius:10px;padding:12px;overflow-y:auto;font-family:var(--mono);font-size:11.5px;line-height:1.65;border:1px solid rgba(255,255,255,.06)}
.con-out::-webkit-scrollbar{width:5px}
.con-out::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:3px}
.cl-info{color:#9cdcfe}.cl-warn{color:#dcdcaa}.cl-err{color:#f44747}.cl-panel{color:#00e5ff}.cl-pool{color:#7c6aff}.cl-def{color:#d4d4d4}
.con-in{display:flex;gap:8px;margin-top:8px}
.con-input{flex:1;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--t1);border-radius:8px;padding:9px 12px;font-family:var(--mono);font-size:12px;outline:none}
.con-input:focus{border-color:rgba(0,229,255,.4)}
.con-send{padding:9px 20px;background:linear-gradient(135deg,var(--a1),var(--a2));color:#000;border:none;border-radius:8px;font-weight:700;cursor:pointer;font-family:var(--font)}
.prog{height:5px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden;margin-top:5px}
.prog-f{height:100%;border-radius:3px;transition:width .5s}
.pf-cpu{background:linear-gradient(90deg,var(--a1),var(--a2))}
.pf-ram{background:linear-gradient(90deg,var(--a3),var(--a1))}
.pf-disk{background:linear-gradient(90deg,var(--a2),#ff6b35)}
.pf-pool{background:linear-gradient(90deg,var(--a2),var(--a1))}
.inp{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--t1);border-radius:8px;padding:8px 12px;font-family:var(--font);font-size:12px;outline:none}
.inp:focus{border-color:rgba(0,229,255,.4)}
.inp-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
/* Pool agent card */
.agent-card{background:var(--s2);border:1px solid rgba(124,106,255,.2);border-radius:12px;padding:16px;margin-bottom:12px}
.agent-hd{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.agent-res{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.res-box{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:10px 8px;text-align:center}
.res-val{font-size:16px;font-weight:700;font-family:var(--mono);color:var(--a1)}
.res-lbl{font-size:9px;color:var(--t2);margin-top:3px;text-transform:uppercase;letter-spacing:.06em}
.notif-wrap{position:fixed;top:14px;right:14px;z-index:200;display:flex;flex-direction:column;gap:8px}
.notif{padding:10px 16px;border-radius:10px;font-size:12px;font-weight:600;max-width:300px;animation:slide-in .3s ease}
@keyframes slide-in{from{transform:translateX(120px);opacity:0}to{transform:none;opacity:1}}
.n-ok{background:rgba(46,213,115,.15);border:1px solid rgba(46,213,115,.3);color:var(--green)}
.n-err{background:rgba(255,71,87,.15);border:1px solid rgba(255,71,87,.3);color:var(--red)}
.n-info{background:rgba(0,229,255,.1);border:1px solid rgba(0,229,255,.25);color:var(--a1)}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:3px}
input[type=file]{display:none}
.fm{display:flex;gap:12px;height:calc(100vh - 160px)}
.fm-tree{width:280px;flex-shrink:0;display:flex;flex-direction:column;background:var(--s1);border:1px solid rgba(255,255,255,.06);border-radius:var(--r);overflow:hidden}
.fm-toolbar{padding:10px 12px;border-bottom:1px solid rgba(255,255,255,.06);display:flex;gap:6px;align-items:center}
.fm-bread{font-size:11px;color:var(--t2);font-family:var(--mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fm-list{flex:1;overflow-y:auto}
.fm-item{display:flex;align-items:center;gap:8px;padding:7px 12px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.03);transition:background .1s;font-size:12px}
.fm-item:hover{background:rgba(255,255,255,.04)}
.fm-item.sel{background:rgba(0,229,255,.07);border-left:2px solid var(--a1)}
.fm-ico{font-size:14px;width:18px;text-align:center}
.fm-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fm-size{font-size:10px;color:var(--t3);font-family:var(--mono)}
.fm-editor{flex:1;display:flex;flex-direction:column;background:var(--s1);border:1px solid rgba(255,255,255,.06);border-radius:var(--r);overflow:hidden}
.fm-etool{padding:9px 12px;border-bottom:1px solid rgba(255,255,255,.06);display:flex;gap:6px;align-items:center}
.fm-fname{font-family:var(--mono);font-size:11px;color:var(--t2);flex:1}
.fm-area{flex:1;background:#1e1e1e;color:#d4d4d4;font-family:var(--mono);font-size:12px;border:none;outline:none;padding:14px;resize:none;line-height:1.6}
.set-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.set-item{background:var(--s2);border:1px solid rgba(255,255,255,.05);border-radius:9px;padding:12px}
.set-lbl{font-size:10px;color:var(--t2);margin-bottom:5px;text-transform:uppercase;letter-spacing:.06em}
.set-inp{width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);color:var(--t1);border-radius:7px;padding:7px 9px;font-family:var(--mono);font-size:12px;outline:none}
.set-inp:focus{border-color:rgba(0,229,255,.4)}
select.set-inp option{background:#1e1e1e}
</style>
</head>
<body>
<div class="layout">
<div class="sidebar">
  <div class="sb-head">
    <h2>⛏️ MC Panel</h2>
    <div class="sb-ver" id="sb-ver">Cuberite • 1.8.8</div>
  </div>
  <div class="sb-status">
    <div class="dot dot-red" id="status-dot"></div>
    <span id="status-text" style="font-size:12px">Durduruldu</span>
  </div>
  <nav class="nav">
    <div class="nav-sec">Genel</div>
    <div class="nav-item active" data-page="dashboard"><span class="ico">📊</span>Dashboard</div>
    <div class="nav-item" data-page="console"><span class="ico">💻</span>Konsol</div>
    <div class="nav-sec">Oyuncular</div>
    <div class="nav-item" data-page="players"><span class="ico">👥</span>Online Oyuncular</div>
    <div class="nav-item" data-page="whitelist"><span class="ico">📋</span>Beyaz Liste</div>
    <div class="nav-item" data-page="banlist"><span class="ico">🔨</span>Ban Listesi</div>
    <div class="nav-sec">Sunucu</div>
    <div class="nav-item" data-page="plugins"><span class="ico">🔌</span>Pluginler</div>
    <div class="nav-item" data-page="files"><span class="ico">📁</span>Dosyalar</div>
    <div class="nav-item" data-page="worlds"><span class="ico">🌍</span>Dünyalar</div>
    <div class="nav-item" data-page="backups"><span class="ico">💾</span>Yedekler</div>
    <div class="nav-item" data-page="settings"><span class="ico">⚙️</span>Ayarlar</div>
    <div class="nav-sec">İzleme</div>
    <div class="nav-item" data-page="perf"><span class="ico">📈</span>Performans</div>
    <div class="nav-item" data-page="pool"><span class="ico">🔗</span>Kaynak Havuzu <span id="pool-badge" style="background:rgba(124,106,255,.2);color:var(--a2);border-radius:20px;padding:1px 6px;font-size:10px;margin-left:auto">0</span></div>
  </nav>
  <div class="sb-ctrl">
    <button class="ctrl-btn cb-start"   onclick="srvAction('start')">▶ Başlat</button>
    <button class="ctrl-btn cb-restart" onclick="srvAction('restart')">↺ Yeniden Başlat</button>
    <button class="ctrl-btn cb-stop"    onclick="srvAction('stop')">■ Durdur</button>
  </div>
</div>
<div class="main">
  <div class="topbar">
    <div class="page-title" id="page-title">📊 Dashboard</div>
    <div class="top-stats">
      <div class="ts">🔗 <span class="ts-v" id="tb-agents">0</span> agent</div>
      <div class="ts">🧠 <span class="ts-v" id="tb-cache">0MB</span> cache</div>
      <div class="ts">👥 <span class="ts-v" id="tb-pl">0</span></div>
      <div class="ts">⚡ <span class="ts-v" id="tb-tps">20.0</span> TPS</div>
      <div class="ts">🧠 <span class="ts-v" id="tb-ram">—MB</span></div>
    </div>
  </div>
  <div class="mc-addr-bar hidden" id="mc-addr-bar">
    <span style="color:var(--t2)">📌 MC Adresi:</span>
    <span id="mc-addr-text" style="color:var(--a3);font-family:var(--mono);font-weight:600">bekleniyor...</span>
    <button class="btn btn-sm b-ghost" onclick="copyAddr()">📋 Kopyala</button>
  </div>
  <div class="pool-bar hidden" id="pool-bar">
    <span style="color:var(--a2)">🔗 Aktif Agentlar:</span>
    <span id="pool-bar-text" style="color:var(--t2);font-size:11px">bağlı agent yok</span>
    <span id="pool-res-bar" style="color:var(--a1);font-family:var(--mono);font-size:10px;margin-left:auto"></span> &nbsp; <span id="pool-sync-info" style="color:var(--t2);font-size:10px"></span>
  </div>
  <div class="pages">

  <!-- DASHBOARD -->
  <div class="page active" id="page-dashboard">
    <div class="g4" style="margin-bottom:14px">
      <div class="sc"><div class="sc-val" id="d-pl">0</div><div class="sc-lbl">👥 Online</div></div>
      <div class="sc"><div class="sc-val" id="d-tps">20.0</div><div class="sc-lbl">⚡ TPS</div></div>
      <div class="sc"><div class="sc-val" id="d-ram">—</div><div class="sc-lbl">🧠 MC RAM MB</div></div>
      <div class="sc"><div class="sc-val" id="d-agents">0</div><div class="sc-lbl">🔗 Agentlar</div></div>
    </div>
    <!-- Pool kaynak özeti -->
    <div class="card" id="pool-summary-card" style="display:none;background:linear-gradient(135deg,rgba(124,106,255,.08),rgba(0,229,255,.05));border-color:rgba(124,106,255,.25)">
      <div class="card-hd" style="color:var(--a2)">🔗 Kaynak Havuzu</div>
      <div class="g4">
        <div class="res-box"><div class="res-val" id="ds-cache">0MB</div><div class="res-lbl">RAM Cache</div></div>
        <div class="res-box"><div class="res-val" id="ds-disk">0GB</div><div class="res-lbl">Disk Deposu</div></div>
        <div class="res-box"><div class="res-val" id="ds-cpu">0</div><div class="res-lbl">CPU Core</div></div>
        <div class="res-box"><div class="res-val" id="ds-proxy">0</div><div class="res-lbl">Proxy Agent</div></div>
      </div>
    </div>
    <div class="g2">
      <div class="card">
        <div class="card-hd">Sunucu Bilgisi</div>
        <table class="tbl">
          <tr><td style="color:var(--t2)">Durum</td><td><span class="badge bg" id="d-status">—</span></td></tr>
          <tr><td style="color:var(--t2)">Versiyon</td><td id="d-ver">—</td></tr>
          <tr><td style="color:var(--t2)">TPS (1m/5m/15m)</td><td id="d-tps3">—</td></tr>
          <tr><td style="color:var(--t2)">Bağlantı</td><td><span id="d-addr" style="font-family:var(--mono);font-size:11px;color:var(--a3)">—</span></td></tr>
          <tr><td style="color:var(--t2)">Agent Kaynakları</td><td id="d-poolinfo" style="color:var(--a2)">Bekleniyor...</td></tr>
        </table>
      </div>
      <div class="card">
        <div class="card-hd">Online Oyuncular</div>
        <div id="d-pllist" style="font-size:13px;color:var(--t2)">Sunucu çalışmıyor</div>
      </div>
    </div>
    <div class="card">
      <div class="card-hd" style="justify-content:space-between">
        <span>Son Konsol</span>
        <a class="btn btn-sm b-ghost" onclick="navTo('console')">Konsola Git →</a>
      </div>
      <div id="d-log" style="font-family:var(--mono);font-size:11px;max-height:180px;overflow-y:auto;line-height:1.7;color:#9cdcfe"></div>
    </div>
  </div>

  <!-- KONSOL -->
  <div class="page" id="page-console" style="height:100%;display:none;flex-direction:column;padding:14px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div class="card-hd" style="margin:0">💻 Gerçek Zamanlı Konsol</div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm b-ghost" onclick="conClear()">🗑 Temizle</button>
        <button class="btn btn-sm b-ghost" onclick="conBottom()">↓ En Alta</button>
      </div>
    </div>
    <div class="con-wrap" style="flex:1">
      <div class="con-out" id="con-out"></div>
      <div class="con-in">
        <input class="con-input" id="con-inp" placeholder="Komut gir...">
        <button class="con-send" onclick="conSend()">▶ Gönder</button>
      </div>
    </div>
  </div>

  <!-- OYUNCULAR -->
  <div class="page" id="page-players">
    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">👥 Online Oyuncular</div>
        <button class="btn btn-sm b-ghost" onclick="refreshPlayers()">↺ Yenile</button>
      </div>
      <table class="tbl"><thead><tr><th>Oyuncu</th><th>Durum</th><th>İşlemler</th></tr></thead><tbody id="pl-body"></tbody></table>
    </div>
    <div class="g2">
      <div class="card">
        <div class="card-hd">⚡ Hızlı İşlem</div>
        <div class="inp-row"><input class="inp" id="pl-name" placeholder="Oyuncu adı" style="flex:1"></div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          <button class="btn btn-sm b-dang" onclick="plAct('kick')">Kick</button>
          <button class="btn btn-sm b-dang" onclick="plAct('ban')">Ban</button>
          <button class="btn btn-sm b-succ" onclick="plAct('op')">OP</button>
          <button class="btn btn-sm b-warn" onclick="plAct('deop')">DeOP</button>
          <button class="btn btn-sm b-ghost" onclick="plAct('heal')">Heal</button>
          <button class="btn btn-sm b-dang" onclick="plAct('kill')">Kill</button>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
          <button class="btn btn-sm b-ghost" onclick="setGM('survival')">Survival</button>
          <button class="btn btn-sm b-ghost" onclick="setGM('creative')">Creative</button>
          <button class="btn btn-sm b-ghost" onclick="setGM('spectator')">Spectator</button>
        </div>
      </div>
      <div class="card">
        <div class="card-hd">📩 Mesaj & Give</div>
        <input class="inp" id="msg-pl" placeholder="Oyuncu" style="width:100%;margin-bottom:6px">
        <input class="inp" id="msg-txt" placeholder="Mesaj" style="width:100%;margin-bottom:6px">
        <button class="btn b-prim btn-sm" style="width:100%;margin-bottom:10px" onclick="sendMsg()">📩 Gönder</button>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <input class="inp" id="give-item" placeholder="Item" style="flex:2">
          <input class="inp" id="give-count" type="number" value="1" style="width:70px">
          <button class="btn b-prim btn-sm" onclick="giveItem()">🎁 Give</button>
        </div>
      </div>
    </div>
  </div>

  <!-- BEYAZ LİSTE -->
  <div class="page" id="page-whitelist">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">📋 Beyaz Liste</div>
        <div class="inp-row" style="margin:0;gap:6px">
          <input class="inp" id="wl-name" placeholder="Oyuncu" style="width:140px">
          <button class="btn btn-sm b-succ" onclick="wlAdd()">+ Ekle</button>
        </div>
      </div>
      <table class="tbl"><thead><tr><th>Oyuncu</th><th>UUID</th><th>İşlem</th></tr></thead><tbody id="wl-body"></tbody></table>
    </div>
  </div>

  <!-- BAN LİSTESİ -->
  <div class="page" id="page-banlist">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">🔨 Ban Listesi</div>
        <div class="inp-row" style="margin:0;gap:6px">
          <input class="inp" id="ban-name" placeholder="Oyuncu" style="width:130px">
          <input class="inp" id="ban-reason" placeholder="Sebep" style="width:130px">
          <button class="btn btn-sm b-dang" onclick="banPlayer()">🔨 Ban</button>
        </div>
      </div>
      <table class="tbl"><thead><tr><th>Oyuncu</th><th>Sebep</th><th>Tarih</th><th>İşlem</th></tr></thead><tbody id="ban-body"></tbody></table>
    </div>
  </div>

  <!-- PLUGİNLER -->
  <div class="page" id="page-plugins">
    <div class="g2" style="height:calc(100vh-150px)">
      <div class="card" style="display:flex;flex-direction:column;overflow:hidden">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-shrink:0">
          <div class="card-hd" style="margin:0">🔌 Kurulu Pluginler</div>
          <div>
            <label for="plug-up" class="btn btn-sm b-prim" style="cursor:pointer">⬆ Yükle</label>
            <input type="file" id="plug-up" accept=".jar" multiple onchange="uploadPlugin(this)">
            <button class="btn btn-sm b-ghost" onclick="loadPlugins()">↺</button>
          </div>
        </div>
        <div style="flex:1;overflow-y:auto">
          <table class="tbl"><thead><tr><th>Plugin</th><th>Boyut</th><th>Durum</th><th>İşlem</th></tr></thead><tbody id="plug-body"></tbody></table>
        </div>
      </div>
      <div class="card" style="display:flex;flex-direction:column;overflow:hidden">
        <div class="card-hd">🔍 Plugin Market</div>
        <div style="display:flex;gap:6px;margin-bottom:12px;flex-shrink:0">
          <input class="inp" id="plug-q" placeholder="Ara..." style="flex:1">
          <button class="btn b-prim" onclick="searchPlugins()">Ara</button>
        </div>
        <div id="plug-results" style="flex:1;overflow-y:auto"></div>
      </div>
    </div>
  </div>

  <!-- DOSYALAR -->
  <div class="page" id="page-files" style="height:100%;padding:14px">
    <div class="fm">
      <div class="fm-tree">
        <div class="fm-toolbar">
          <span class="fm-bread" id="fm-bread">/</span>
          <button class="btn btn-sm b-ghost" onclick="fmUp()">↑</button>
          <button class="btn btn-sm b-ghost" onclick="fmRefresh()">↺</button>
          <button class="btn btn-sm b-prim" onclick="fmNewModal()">+</button>
        </div>
        <div class="fm-list" id="fm-list"></div>
      </div>
      <div class="fm-editor">
        <div class="fm-etool">
          <span class="fm-fname" id="fm-fname">Dosya seçin...</span>
          <button class="btn btn-sm b-prim" id="fm-save" onclick="fmSave()" disabled>💾 Kaydet</button>
          <button class="btn btn-sm b-ghost" onclick="fmDownload()">⬇</button>
          <label class="btn btn-sm b-ghost" style="cursor:pointer">⬆ <input type="file" multiple onchange="fmUpload(this)"></label>
          <button class="btn btn-sm b-dang" onclick="fmDelete()">🗑</button>
        </div>
        <textarea class="fm-area" id="fm-area" placeholder="Düzenlemek için sol panelden dosya seçin..." oninput="document.getElementById('fm-save').disabled=false"></textarea>
      </div>
    </div>
  </div>

  <!-- DÜNYALAR -->
  <div class="page" id="page-worlds">
    <div class="card" style="margin-bottom:14px">
      <div class="card-hd">🌍 Dünya Listesi</div>
      <div id="worlds-list"></div>
    </div>
    <div class="card">
      <div class="card-hd">⚡ Dünya Komutları</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <button class="btn b-ghost" onclick="cmd('time set day')">☀️ Gündüz</button>
        <button class="btn b-ghost" onclick="cmd('time set night')">🌙 Gece</button>
        <button class="btn b-ghost" onclick="cmd('weather clear')">⛅ Açık</button>
        <button class="btn b-ghost" onclick="cmd('weather rain')">🌧️ Yağmur</button>
        <button class="btn b-ghost" onclick="cmd('difficulty peaceful')">😊 Peaceful</button>
        <button class="btn b-ghost" onclick="cmd('difficulty hard')">🔴 Hard</button>
        <button class="btn b-ghost" onclick="cmd('save')">💾 Kaydet</button>
        <button class="btn b-ghost" onclick="cmd('kill @e[type=!player]')">⚡ Mob Temizle</button>
      </div>
    </div>
  </div>

  <!-- YEDEKLER -->
  <div class="page" id="page-backups">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">💾 Yedekler</div>
        <button class="btn b-prim" onclick="loadBackups()">↺ Yenile</button>
      </div>
      <table class="tbl"><thead><tr><th>Dosya</th><th>Boyut</th><th>Tarih</th><th>İşlem</th></tr></thead><tbody id="backup-body"></tbody></table>
    </div>
  </div>

  <!-- AYARLAR -->
  <div class="page" id="page-settings">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div class="card-hd" style="margin:0">⚙️ server.properties</div>
        <button class="btn b-prim btn-lg" onclick="saveSettings()">💾 Kaydet & Yeniden Başlat</button>
      </div>
      <div class="set-grid" id="settings-grid"></div>
    </div>
  </div>

  <!-- PERFORMANS -->
  <div class="page" id="page-perf">
    <div class="g2" style="margin-bottom:14px">
      <div class="card">
        <div class="card-hd">💻 Sistem</div>
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px"><span>CPU</span><span id="p-cpu">—</span></div>
          <div class="prog"><div class="prog-f pf-cpu" id="pb-cpu" style="width:0%"></div></div>
        </div>
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px"><span>RAM</span><span id="p-ram">—</span></div>
          <div class="prog"><div class="prog-f pf-ram" id="pb-ram" style="width:0%"></div></div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px"><span>Disk</span><span id="p-disk">—</span></div>
          <div class="prog"><div class="prog-f pf-disk" id="pb-disk" style="width:0%"></div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-hd">⛏️ MC + Pool</div>
        <table class="tbl">
          <tr><td style="color:var(--t2)">MC RAM</td><td id="p-mcram">—</td></tr>
          <tr><td style="color:var(--t2)">TPS (1m/5m/15m)</td><td id="p-tps1">—</td></tr>
          <tr><td style="color:var(--t2)">Swap</td><td id="p-swap">—</td></tr>
          <tr><td style="color:var(--t2)">Pool Agentlar</td><td id="p-agents">—</td></tr>
          <tr><td style="color:var(--t2)">Pool RAM Cache</td><td id="p-pcache">—</td></tr>
          <tr><td style="color:var(--t2)">Pool Disk Deposu</td><td id="p-pdisk">—</td></tr>
        </table>
      </div>
    </div>
  </div>

  <!-- KAYNAK HAVUZU -->
  <div class="page" id="page-pool">
    <!-- Özet bar -->
    <div class="g4" style="margin-bottom:14px" id="pool-top-stats">
      <div class="sc"><div class="sc-val" id="ps-agents">0</div><div class="sc-lbl">🔗 Aktif Agent</div></div>
      <div class="sc"><div class="sc-val" id="ps-cache">0MB</div><div class="sc-lbl">🧠 RAM Cache</div></div>
      <div class="sc"><div class="sc-val" id="ps-disk">0GB</div><div class="sc-lbl">💾 Disk Deposu</div></div>
      <div class="sc"><div class="sc-val" id="ps-cpu">0</div><div class="sc-lbl">⚡ CPU Core</div></div>
    </div>

    <!-- Aksiyonlar -->
    <div class="card" style="margin-bottom:14px">
      <div class="card-hd">🛠️ Havuz İşlemleri</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <button class="btn b-purp" onclick="poolAction('archive','Eski region arşivleniyor...')">📦 Region Arşivle (5+ gün)</button>
        <button class="btn b-purp" onclick="poolAction('proxy','Proxy başlatılıyor...')">🔀 Proxy Başlat</button>
        <button class="btn b-ghost" onclick="poolAction('flush','Cache temizleniyor...')">🗑️ Cache Temizle</button>
        <button class="btn b-ghost" onclick="loadPoolStatus()">↺ Yenile</button>
      </div>
    </div>

    <!-- Agent listesi -->
    <div id="pool-agents-list">
      <div style="text-align:center;padding:40px;color:var(--t2)">
        <div style="font-size:36px;margin-bottom:12px">🔗</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">Henüz agent bağlı değil</div>
        <div style="font-size:12px">Başka bir Render hesabına <strong>agent.py</strong> + <strong>agent.Dockerfile</strong> yükleyin</div>
      </div>
    </div>
  </div>

  </div><!-- /pages -->
</div>
</div>

<div class="notif-wrap" id="notif-wrap"></div>

<script>
const socket = io({transports:['websocket','polling']});
let curPage='dashboard', curFile=null, curDir='', mcAddr='', poolData={total:0,healthy:0,resources:{},agents:[]};

socket.on('connect',        ()   => notify('Panele bağlandı','ok'));
socket.on('disconnect',     ()   => notify('Bağlantı kesildi','err'));
socket.on('console_line',   d    => addLine(d));
socket.on('console_history',ls   => {document.getElementById('con-out').innerHTML='';ls.forEach(l=>addLine(l,false));conBottom()});
socket.on('server_status',  d    => updateStatus(d));
socket.on('players_update', l    => updatePlayers(l));
socket.on('stats_update',   d    => updateStats(d));
socket.on('tunnel_update',  d    => setTunnel(d));
socket.on('pool_update',    d    => updatePool(d));
socket.on('download_progress', d => notify(`⬇ %${d.pct}`,'info'));

document.querySelectorAll('.nav-item').forEach(el=>el.addEventListener('click',()=>{const p=el.dataset.page;if(p)navTo(p,el)}));
const TITLES={dashboard:'📊 Dashboard',console:'💻 Konsol',players:'👥 Oyuncular',whitelist:'📋 Beyaz Liste',banlist:'🔨 Ban Listesi',plugins:'🔌 Pluginler',files:'📁 Dosyalar',worlds:'🌍 Dünyalar',backups:'💾 Yedekler',settings:'⚙️ Ayarlar',perf:'📈 Performans',pool:'🔗 Kaynak Havuzu'};
const LOADERS={players:refreshPlayers,whitelist:loadWhitelist,banlist:loadBanlist,plugins:loadPlugins,files:()=>fmLoad(curDir),worlds:loadWorlds,backups:loadBackups,settings:loadSettings,perf:loadPerf,pool:loadPoolStatus};

function navTo(page,el){
  document.querySelectorAll('.page').forEach(p=>{p.classList.remove('active');p.style.display='';});
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const pg=document.getElementById('page-'+page);if(!pg)return;
  pg.classList.add('active');if(page==='console')pg.style.display='flex';
  const nav=el||document.querySelector(`.nav-item[data-page="${page}"]`);if(nav)nav.classList.add('active');
  document.getElementById('page-title').textContent=TITLES[page]||page;
  curPage=page;if(LOADERS[page])LOADERS[page]();
}

function addLine(data,scroll=true){
  const el=document.getElementById('con-out');const div=document.createElement('div');const l=data.line||'';
  let cls='cl-def';
  if(l.includes('[Pool]'))cls='cl-pool';
  else if(l.includes('[Panel]'))cls='cl-panel';
  else if(/error|exception/i.test(l))cls='cl-err';
  else if(/warn/i.test(l))cls='cl-warn';
  else if(l.includes('INFO'))cls='cl-info';
  div.className=cls;div.textContent=`[${data.ts}] ${l}`;el.appendChild(div);
  if(scroll)el.scrollTop=el.scrollHeight;
  const dl=document.getElementById('d-log');if(dl){const s=document.createElement('div');s.textContent=div.textContent;s.className=cls;dl.appendChild(s);while(dl.children.length>30)dl.removeChild(dl.firstChild);dl.scrollTop=dl.scrollHeight;}
}
function conClear(){document.getElementById('con-out').innerHTML='';}
function conBottom(){const e=document.getElementById('con-out');e.scrollTop=e.scrollHeight;}
function conSend(){const inp=document.getElementById('con-inp');const c=inp.value.trim();if(!c)return;socket.emit('send_command',{cmd:c});inp.value='';}
document.addEventListener('DOMContentLoaded',()=>{document.getElementById('con-inp').addEventListener('keydown',e=>{if(e.key==='Enter')conSend();});});

function updateStatus(d){
  const map={stopped:['dot-red','Durduruldu','br'],starting:['dot-yellow','Başlıyor...','by'],downloading:['dot-yellow','İndiriliyor','by'],running:['dot-green','Çalışıyor','bg'],stopping:['dot-yellow','Duruyor','by']};
  const[dc,label,bc]=map[d.status]||map.stopped;
  document.getElementById('status-dot').className='dot '+dc;
  document.getElementById('status-text').textContent=label;
  const b=document.getElementById('d-status');if(b){b.className='badge '+bc;b.textContent=label;}
  const v=document.getElementById('sb-ver');if(v&&d.version&&d.version!=='—')v.textContent='Cuberite • 1.8.8';
  const dv=document.getElementById('d-ver');if(dv)dv.textContent=d.version||'—';
}
function updateStats(d){
  if(d.ram_mb!==undefined){document.getElementById('d-ram').textContent=d.ram_mb;document.getElementById('tb-ram').textContent=d.ram_mb+'MB';}
  if(d.tps!==undefined){document.getElementById('d-tps').textContent=d.tps;document.getElementById('tb-tps').textContent=d.tps;document.getElementById('d-tps3').textContent=`${d.tps}/${d.tps5||'—'}/${d.tps15||'—'}`;}
  if(d.online_players!==undefined){document.getElementById('d-pl').textContent=d.online_players;document.getElementById('tb-pl').textContent=d.online_players;}
}
function setTunnel(d){
  if(!d.host&&!d.url)return;
  mcAddr=d.host||d.url.replace('https://','');
  document.getElementById('mc-addr-bar').classList.remove('hidden');
  document.getElementById('mc-addr-text').textContent=mcAddr;
  const da=document.getElementById('d-addr');if(da)da.textContent=mcAddr;
  notify('📌 MC: '+mcAddr,'ok');
}
function copyAddr(){if(mcAddr){navigator.clipboard.writeText(mcAddr);notify('Kopyalandı!','ok');}}

function updatePool(d){
  poolData=d||{total:0,healthy:0,resources:{},agents:[]};
  const h=poolData.healthy||0,res=poolData.resources||{};
  const cacheMB=res.cache_used_mb||0,diskGB=res.disk_free_gb||0,cpu=res.cpu_cores||0;
  // topbar
  document.getElementById('tb-agents').textContent=h;
  document.getElementById('tb-cache').textContent=cacheMB+'MB';
  document.getElementById('pool-badge').textContent=h;
  document.getElementById('d-agents').textContent=h;
  document.getElementById('d-poolinfo').textContent=h>0?`${h} agent | Cache:${cacheMB}MB | Disk:${diskGB}GB`:'Agent bağlı değil';
  // pool bar
  const bar=document.getElementById('pool-bar'),bt=document.getElementById('pool-bar-text'),br=document.getElementById('pool-res-bar');
  if(h>0){
    bar.classList.remove('hidden');
    bt.textContent=(poolData.agents||[]).map(a=>a.node_id.split('-')[0]).join(' · ');
    br.textContent=`Cache:${cacheMB}MB  Disk:${diskGB}GB  CPU:${cpu}c`;
  } else bar.classList.add('hidden');
  // dashboard summary card
  const sc=document.getElementById('pool-summary-card');
  if(sc){sc.style.display=h>0?'block':'none';}
  const ds={cache:`${cacheMB}MB`,disk:`${diskGB}GB`,cpu:`${cpu}`,proxy:`${(poolData.agents||[]).filter(a=>a.proxy&&a.proxy.active).length}`};
  ['cache','disk','cpu','proxy'].forEach(k=>{const el=document.getElementById('ds-'+k);if(el)el.textContent=ds[k];});
  // pool page
  ['agents','cache','disk','cpu'].forEach(k=>{
    const el=document.getElementById('ps-'+k);
    if(el)el.textContent=k==='agents'?h:k==='cache'?cacheMB+'MB':k==='disk'?diskGB+'GB':cpu;
  });
  if(curPage==='pool')renderPoolAgents(poolData.agents||[]);
}

function renderPoolAgents(agents){
  const el=document.getElementById('pool-agents-list');if(!el)return;
  if(!agents.length){el.innerHTML='<div style="text-align:center;padding:40px;color:var(--t2)"><div style="font-size:36px;margin-bottom:12px">🔗</div><div style="font-size:14px;font-weight:600">Henüz agent bağlı değil</div></div>';return;}
  el.innerHTML=agents.map(a=>{
    const ram=a.ram||{},disk=a.disk||{},cpu=a.cpu||{},proxy=a.proxy||{};
    const cacheUsed=ram.cache_mb||0,ramFree=ram.free_mb||0,diskFree=disk.free_gb||0,diskStore=disk.store_gb||0,cores=cpu.cores||0,load=cpu.load1||0;
    return `<div class="agent-card">
      <div class="agent-hd">
        <div class="dot ${a.healthy?'dot-green':'dot-red'}"></div>
        <strong style="font-size:13px;font-family:var(--mono)">${a.node_id}</strong>
        <span class="badge ${a.healthy?'bg':'br'}" style="margin-left:8px">${a.healthy?'Sağlıklı':'Erişilemiyor'}</span>
        ${proxy.active?'<span class="badge bp" style="margin-left:4px">🔀 Proxy Aktif</span>':''}
        <span style="margin-left:auto;font-size:10px;color:var(--t3)">${a.url||''}</span>
      </div>
      <div class="agent-res">
        <div class="res-box">
          <div class="res-val" style="color:var(--a1)">${cacheUsed}MB</div>
          <div class="res-lbl">🧠 RAM Cache</div>
          <div style="font-size:9px;color:var(--t3);margin-top:2px">${ramFree}MB boş</div>
        </div>
        <div class="res-box">
          <div class="res-val" style="color:var(--a3)">${diskStore}GB</div>
          <div class="res-lbl">💾 Disk Deposu</div>
          <div style="font-size:9px;color:var(--t3);margin-top:2px">${diskFree}GB boş</div>
        </div>
        <div class="res-box">
          <div class="res-val" style="color:var(--a2)">${cores}</div>
          <div class="res-lbl">⚡ CPU Core</div>
          <div style="font-size:9px;color:var(--t3);margin-top:2px">Load: ${load}</div>
        </div>
        <div class="res-box">
          <div class="res-val" style="color:${proxy.active?'var(--green)':'var(--t3)'}">${proxy.active?'Açık':'Kapalı'}</div>
          <div class="res-lbl">🔀 Proxy</div>
          <div style="font-size:9px;color:var(--t3);margin-top:2px">${proxy.connections||0} bağlantı</div>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function loadSyncStatus(){
  try {
    const d = await fetch('/api/pool/sync/status').then(r=>r.json());
    const el = document.getElementById('pool-sync-info');
    if(el) el.textContent = `📦 ${d.offloaded} offload | 🔄 ${d.synced} sync | Disk:${d.agent_disk_used_gb}GB | RAM:${d.agent_ram_cache_mb}MB`;
  } catch(e) {}
}
setInterval(loadSyncStatus, 30000);

async function loadPoolStatus(){
  const d=await fetch('/api/pool/status').then(r=>r.json()).catch(()=>({}));
  updatePool(d);
}

async function poolAction(action,msg){
  notify(msg,'info');
  let url,body={};
  if(action==='archive'){url='/api/pool/archive/regions';body={older_than_days:5};}
  else if(action==='proxy'){url='/api/pool/proxy/start';body={host:'127.0.0.1',port:25565};}
  else if(action==='flush'){url='/api/pool/cache/flush';body={};}
  else return;
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json()).catch(()=>({ok:false}));
  notify(r.ok?(action==='archive'?`✅ ${r.archived||0} region arşivlendi (${r.freed_mb||0}MB)`:`✅ ${action} tamam`):'❌ İşlem başarısız',r.ok?'ok':'err');
  loadPoolStatus();
}

async function srvAction(a){const r=await api('/api/'+a,{});notify(r.msg||a,'ok');}
function updatePlayers(list){
  document.getElementById('d-pl').textContent=list.length;
  document.getElementById('tb-pl').textContent=list.length;
  const el=document.getElementById('d-pllist');
  if(el)el.innerHTML=list.length?list.map(p=>`<div style="padding:3px 0">🟢 ${p.name}</div>`).join(''):'<span style="color:var(--t2)">Çevrimiçi yok</span>';
  const tb=document.getElementById('pl-body');if(!tb)return;
  tb.innerHTML=list.length?list.map(p=>`<tr><td><strong>${p.name}</strong></td><td><span class="badge bg">Online</span></td><td style="display:flex;gap:4px">
    <button class="btn btn-sm b-dang" onclick="quickAct('kick','${p.name}')">Kick</button>
    <button class="btn btn-sm b-dang" onclick="quickAct('ban','${p.name}')">Ban</button>
    <button class="btn btn-sm b-succ" onclick="quickAct('op','${p.name}')">OP</button>
    <button class="btn btn-sm b-ghost" onclick="setGM('creative','${p.name}')">Creative</button>
  </td></tr>`).join(''):'<tr><td colspan="3" style="color:var(--t2);text-align:center;padding:14px">Çevrimiçi oyuncu yok</td></tr>';
}
async function refreshPlayers(){const d=await api('/api/players');updatePlayers(d.players||[]);}
async function quickAct(a,p){await api('/api/players/'+a,{player:p});notify(`${p} → ${a}`,'ok');}
function plAct(a){const p=document.getElementById('pl-name').value.trim();if(!p)return notify('Oyuncu adı girin','err');quickAct(a,p);}
function setGM(mode,player){const p=player||document.getElementById('pl-name').value.trim();if(!p)return notify('Oyuncu adı girin','err');api('/api/players/gamemode',{player:p,mode});notify(`${p}→${mode}`,'ok');}
function sendMsg(){const p=document.getElementById('msg-pl').value.trim(),m=document.getElementById('msg-txt').value.trim();if(!p||!m)return notify('Oyuncu ve mesaj gerekli','err');api('/api/players/msg',{player:p,message:m});notify('Gönderildi','ok');}
function giveItem(){const player=document.getElementById('pl-name').value.trim()||'@a',item=document.getElementById('give-item').value.trim(),count=document.getElementById('give-count').value||1;if(!item)return notify('Item girin','err');api('/api/players/give',{player,item,count});notify(`Give: ${count}x ${item}`,'ok');}

async function loadWhitelist(){const d=await fetch('/api/whitelist').then(r=>r.json());const tb=document.getElementById('wl-body');tb.innerHTML=d.length?d.map(p=>`<tr><td>${p.name||p}</td><td style="font-family:var(--mono);font-size:10px;color:var(--t2)">${p.uuid||'—'}</td><td><button class="btn btn-sm b-dang" onclick="wlRm('${p.name||p}')">Kaldır</button></td></tr>`).join(''):'<tr><td colspan="3" style="color:var(--t2);text-align:center;padding:12px">Beyaz liste boş</td></tr>';}
async function wlAdd(){const p=document.getElementById('wl-name').value.trim();if(!p)return;await api('/api/whitelist/add',{player:p});notify(p+' eklendi','ok');loadWhitelist();}
async function wlRm(p){await api('/api/whitelist/remove',{player:p});notify(p+' kaldırıldı','ok');loadWhitelist();}
async function loadBanlist(){const d=await fetch('/api/banlist').then(r=>r.json());const tb=document.getElementById('ban-body');tb.innerHTML=d.length?d.map(p=>`<tr><td>${p.name}</td><td style="color:var(--t2);font-size:11px">${p.reason||'—'}</td><td style="font-size:10px;color:var(--t3)">${(p.created||'').slice(0,10)}</td><td><button class="btn btn-sm b-succ" onclick="pardon('${p.name}')">Affet</button></td></tr>`).join(''):'<tr><td colspan="4" style="color:var(--t2);text-align:center;padding:12px">Boş</td></tr>';}
async function banPlayer(){const p=document.getElementById('ban-name').value.trim(),r=document.getElementById('ban-reason').value.trim()||'Banned';if(!p)return;await api('/api/players/ban',{player:p,reason:r});notify(p+' banlandı','ok');loadBanlist();}
async function pardon(p){await api('/api/players/pardon',{player:p});notify(p+' affedildi','ok');loadBanlist();}

async function loadPlugins(){const d=await fetch('/api/plugins').then(r=>r.json());const tb=document.getElementById('plug-body');tb.innerHTML=d.length?d.map(p=>`<tr><td><strong>${p.name}</strong></td><td style="font-size:10px;color:var(--t2)">${fmtSize(p.size)}</td><td><span class="badge ${p.enabled?'bg':'br'}">${p.enabled?'Aktif':'Kapalı'}</span></td><td style="display:flex;gap:4px"><button class="btn btn-sm b-warn" onclick="togglePlugin('${p.file}')">${p.enabled?'Kapat':'Aç'}</button><button class="btn btn-sm b-dang" onclick="deletePlugin('${p.file}')">🗑</button></td></tr>`).join(''):'<tr><td colspan="4" style="color:var(--t2);text-align:center;padding:12px">Plugin yok</td></tr>';}
async function uploadPlugin(input){const fd=new FormData();for(const f of input.files)fd.append(f.name,f);const r=await fetch('/api/plugins/upload',{method:'POST',body:fd}).then(r=>r.json());notify(r.msg||'Yüklendi','ok');loadPlugins();}
async function deletePlugin(file){if(!confirm(file+'?'))return;await api('/api/plugins/delete',{file});notify('Silindi','ok');loadPlugins();}
async function togglePlugin(file){await api('/api/plugins/toggle',{file});loadPlugins();}
async function searchPlugins(){const q=document.getElementById('plug-q').value.trim();if(!q)return;const res=document.getElementById('plug-results');res.innerHTML='<div style="color:var(--t2);padding:10px">Aranıyor...</div>';const d=await fetch('/api/plugins/search?q='+encodeURIComponent(q)).then(r=>r.json());if(d.error){res.innerHTML='<div style="color:var(--red);padding:10px">'+d.error+'</div>';return;}if(!d.length){res.innerHTML='<div style="color:var(--t2);padding:10px">Bulunamadı</div>';return;}res.innerHTML=d.map(p=>`<div style="display:flex;justify-content:space-between;align-items:flex-start;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.05)"><div><div style="font-size:13px;font-weight:600">${p.name}</div><div style="font-size:11px;color:var(--t2);margin-top:2px">${p.description}</div><div style="font-size:10px;color:var(--t3);margin-top:2px">👤 ${p.owner} · ⬇ ${(p.downloads||0).toLocaleString()}</div></div><a class="btn btn-sm b-prim" href="${p.url}" target="_blank">🔗 Aç</a></div>`).join('');}

async function fmLoad(path=''){curDir=path;document.getElementById('fm-bread').textContent='/'+path;const items=await fetch('/api/files?path='+encodeURIComponent(path)).then(r=>r.json());const el=document.getElementById('fm-list');el.innerHTML=items.map(f=>`<div class="fm-item" onclick="fmClick('${f.path}','${f.type}','${f.name.replace(/'/g,"\\'")}')"><span class="fm-ico">${f.type==='dir'?'📁':fmIco(f.ext)}</span><span class="fm-name">${f.name}</span><span class="fm-size">${f.type==='dir'?'':fmtSize(f.size)}</span></div>`).join('')||'<div style="padding:12px;color:var(--t2);font-size:12px">Boş</div>';}
function fmIco(ext){const m={'.properties':'⚙️','.json':'📋','.yml':'📋','.yaml':'📋','.jar':'☕','.txt':'📄','.log':'📜','.sh':'🖥️','.zip':'📦','.png':'🖼️','.dat':'🗃️'};return m[ext]||'📄';}
function fmUp(){const parts=curDir.split('/').filter(Boolean);parts.pop();fmLoad(parts.join('/'));}
function fmRefresh(){fmLoad(curDir);}
async function fmClick(path,type,name){document.querySelectorAll('.fm-item').forEach(i=>i.classList.remove('sel'));event.currentTarget.classList.add('sel');curFile=path;if(type==='dir'){fmLoad(path);return;}document.getElementById('fm-fname').textContent=name;const textExts=['.properties','.json','.yml','.yaml','.txt','.log','.sh','.conf','.toml','.cfg','.xml','.md'];const ext='.'+name.split('.').pop().toLowerCase();if(textExts.includes(ext)){const d=await fetch('/api/files/read?path='+encodeURIComponent(path)).then(r=>r.json());document.getElementById('fm-area').value=d.content||'';document.getElementById('fm-save').disabled=false;}else{document.getElementById('fm-area').value='(Binary dosya)';document.getElementById('fm-save').disabled=true;}}
async function fmSave(){if(!curFile)return;const c=document.getElementById('fm-area').value;await api('/api/files/write',{path:curFile,content:c});notify('Kaydedildi','ok');document.getElementById('fm-save').disabled=true;}
function fmDownload(){if(curFile)window.open('/api/files/download?path='+encodeURIComponent(curFile));}
async function fmDelete(){if(!curFile||!confirm(curFile+'?'))return;await api('/api/files/delete',{path:curFile});notify('Silindi','ok');fmLoad(curDir);curFile=null;document.getElementById('fm-area').value='';}
async function fmUpload(input){const fd=new FormData();fd.append('path',curDir);for(const f of input.files)fd.append(f.name,f);await fetch('/api/files/upload',{method:'POST',body:fd});notify('Yüklendi','ok');fmLoad(curDir);}
function fmNewModal(){const name=prompt('Dosya/klasör adı:');if(!name)return;if(name.includes('.'))api('/api/files/write',{path:curDir+'/'+name,content:''}).then(()=>fmLoad(curDir));else api('/api/files/mkdir',{path:curDir+'/'+name}).then(()=>fmLoad(curDir));}

async function loadWorlds(){const d=await fetch('/api/worlds').then(r=>r.json());const el=document.getElementById('worlds-list');el.innerHTML=d.length?d.map(w=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.05)"><div><div style="font-weight:600">🌍 ${w.name}</div><div style="font-size:11px;color:var(--t2);margin-top:2px">${fmtSize(w.size)}</div></div><div style="display:flex;gap:8px"><button class="btn btn-sm b-prim" onclick="bkWorld('${w.name}')">💾 Yedek</button><button class="btn btn-sm b-dang" onclick="delWorld('${w.name}')">🗑</button></div></div>`).join(''):'<div style="color:var(--t2)">Bulunamadı</div>';}
async function bkWorld(name){notify('Yedekleniyor...','info');const d=await api('/api/worlds/backup',{world:name});notify(d.ok?'Yedeklendi':'Hata',d.ok?'ok':'err');loadBackups();}
async function delWorld(name){if(!confirm(name+'?'))return;const d=await api('/api/worlds/delete',{world:name});notify(d.ok?'Silindi':d.error,d.ok?'ok':'err');loadWorlds();}
async function loadBackups(){const d=await fetch('/api/backups').then(r=>r.json());const tb=document.getElementById('backup-body');tb.innerHTML=d.length?d.map(b=>`<tr><td>${b.name}</td><td style="font-size:11px;color:var(--t2)">${fmtSize(b.size)}</td><td style="font-size:11px;color:var(--t3)">${new Date(b.created*1000).toLocaleString('tr')}</td><td><a class="btn btn-sm b-prim" href="/api/files/download?path=${encodeURIComponent(b.path)}">⬇</a></td></tr>`).join(''):'<tr><td colspan="4" style="color:var(--t2);text-align:center;padding:12px">Yedek yok</td></tr>';}

const SET_LABELS={'server-port':'Port','max-players':'Max Oyuncu','online-mode':'Online Mode','gamemode':'Oyun Modu','difficulty':'Zorluk','motd':'MOTD','view-distance':'Görüş','simulation-distance':'Simülasyon','spawn-protection':'Spawn Koruma','allow-flight':'Uçuş','white-list':'Beyaz Liste','enable-command-block':'Komut Bloğu','pvp':'PvP','allow-nether':'Nether','level-name':'Dünya Adı'};
async function loadSettings(){const d=await fetch('/api/settings').then(r=>r.json());const el=document.getElementById('settings-grid');el.innerHTML=Object.entries(d).map(([k,v])=>`<div class="set-item"><div class="set-lbl">${SET_LABELS[k]||k}</div>${v==='true'||v==='false'?`<select class="set-inp" id="s-${k}"><option value="true" ${v==='true'?'selected':''}>true</option><option value="false" ${v==='false'?'selected':''}>false</option></select>`:`<input class="set-inp" id="s-${k}" value="${v.replace(/</g,'&lt;')}">`}</div>`).join('');}
async function saveSettings(){const d=await fetch('/api/settings').then(r=>r.json());const u={};for(const k of Object.keys(d)){const el=document.getElementById('s-'+k);if(el)u[k]=el.value;}const r=await api('/api/settings',u);notify(r.msg||'Kaydedildi','ok');setTimeout(()=>srvAction('restart'),1500);}

async function loadPerf(){const d=await fetch('/api/performance').then(r=>r.json());if(d.cpu!==undefined){document.getElementById('p-cpu').textContent=d.cpu+'%';document.getElementById('pb-cpu').style.width=d.cpu+'%';}if(d.ram_pct!==undefined){document.getElementById('p-ram').textContent=`${d.ram_used_mb}/${d.ram_total_mb}MB`;document.getElementById('pb-ram').style.width=d.ram_pct+'%';}if(d.disk_pct!==undefined){document.getElementById('p-disk').textContent=`${d.disk_used_gb}/${d.disk_total_gb}GB`;document.getElementById('pb-disk').style.width=d.disk_pct+'%';}if(d.mc&&d.mc.ram)document.getElementById('p-mcram').textContent=d.mc.ram+' MB';document.getElementById('p-tps1').textContent=`${d.tps||'—'} / ${d.tps5||'—'} / ${d.tps15||'—'}`;document.getElementById('p-swap').textContent=`${d.swap_total_mb||0}MB / ${d.swap_free_mb||0}MB boş`;document.getElementById('p-agents').textContent=`${d.pool_agents||0} agent`;document.getElementById('p-pcache').textContent=`${d.pool_cache_mb||0}MB`;document.getElementById('p-pdisk').textContent=`${d.pool_disk_gb||0}GB`;}
setInterval(()=>{if(curPage==='perf')loadPerf();},5000);

function cmd(c){socket.emit('send_command',{cmd:c});notify('→ '+c,'info');}
async function api(url,body={}){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return r.json();}
function fmtSize(b){if(b>1e9)return(b/1e9).toFixed(2)+'GB';if(b>1e6)return(b/1e6).toFixed(1)+'MB';if(b>1e3)return(b/1e3).toFixed(0)+'KB';return b+'B';}
function notify(msg,type='ok'){const wrap=document.getElementById('notif-wrap');const div=document.createElement('div');div.className='notif n-'+type;div.textContent=msg;wrap.appendChild(div);setTimeout(()=>div.remove(),3500);}

async function init(){
  const d=await fetch('/api/status').then(r=>r.json()).catch(()=>({}));
  updateStatus(d);updateStats(d);
  if(d.players)updatePlayers(d.players);
  if(d.tunnel&&d.tunnel.host)setTunnel(d.tunnel);
  if(d.pool)updatePool(d.pool);
}
init();
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════
#  MC_ONLY — Minimal HTTP API (Flask/SocketIO yok, ~5MB overhead)
# ══════════════════════════════════════════════════════════════

def _run_mc_only():
    """
    MC_ONLY=1: Ana sunucu. Flask hiç başlatılmaz → ~90MB kazanım → Xmx=370MB.
    Python http.server ile minimal REST API sunar.
    Panel agent (WORKER_URL bu sunucuyu gösterir) tüm komutları buraya proxy eder.
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse as _up2

    _log_buf  = []
    _log_lock = threading.Lock()

    def _mc_log(line: str):
        with _log_lock:
            _log_buf.append(line)
            if len(_log_buf) > 500:
                del _log_buf[:100]
        print(f"[MC] {line}", flush=True)
        # Panel agent'e push
        cb = os.environ.get("LOG_CALLBACK_URL", "")
        if cb:
            try:
                _urllib_req.urlopen(_urllib_req.Request(
                    cb, data=json.dumps({"line": line}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                ), timeout=2)
            except Exception:
                pass

    def _reader():
        while mc_process and mc_process.poll() is None:
            try:
                line = mc_process.stdout.readline().decode("utf-8", "replace").rstrip()
                if line:
                    _mc_log(line)
                    m = re.search(r"TPS from last[^:]+:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", line)
                    if m:
                        server_state["tps"]   = float(m.group(1))
                        server_state["tps5"]  = float(m.group(2))
                        server_state["tps15"] = float(m.group(3))
                    if "Done" in line and "For help" in line:
                        server_state["status"] = "running"
                    if "Stopping server" in line:
                        server_state["status"] = "stopping"
            except Exception:
                break
        server_state["status"] = "stopped"

    def _do_start():
        global mc_process
        MC_DIR.mkdir(parents=True, exist_ok=True)
        if not MC_BIN.exists():
            server_state["status"] = "downloading"
            _mc_log("[MC_ONLY] 📥 Cuberite binary indiriliyor...")
            if not download_cuberite():
                server_state["status"] = "stopped"
                return False, "İndirme başarısız"
        write_server_config()
        server_state.update({"status": "starting", "online_players": 0})
        jvm = get_jvm_args()
        def _cs():
            try:
                import resource as _r
                _r.setrlimit(_r.RLIMIT_AS, (_r.RLIM_INFINITY, _r.RLIM_INFINITY))
            except Exception: pass
            try: open("/proc/self/oom_score_adj", "w").write("-900")
            except Exception: pass
        mc_process = subprocess.Popen(
            jvm, cwd=str(MC_DIR),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            preexec_fn=_cs,
        )
        threading.Thread(target=_reader, daemon=True).start()
        _mc_log(f"[MC_ONLY] 🚀 Cuberite PID={mc_process.pid} (~50MB RAM, JVM yok)")
        return True, f"Başlatıldı PID={mc_process.pid}"

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = _up2.urlparse(self.path).path
            if p in ("/", "/health", "/api/ping"):
                self._send(200, {"ok": True, "mode": "MC_ONLY",
                                 "status": server_state.get("status", "stopped")})
            elif p == "/api/mc/status":
                self._send(200, {
                    "ok": True,
                    "status": server_state.get("status", "stopped"),
                    "tps": server_state.get("tps", 0),
                    "tps5": server_state.get("tps5", 0),
                    "tps15": server_state.get("tps15", 0),
                    "players": server_state.get("online_players", 0),
                    "pid": mc_process.pid if mc_process else None,
                })
            elif p == "/api/mc/logs":
                with _log_lock:
                    lines = list(_log_buf[-200:])
                self._send(200, {"ok": True, "lines": lines})
            elif p == "/api/cluster/status":
                self._send(200, vcluster.summary())
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            global mc_process
            p  = _up2.urlparse(self.path).path
            cl = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(cl)) if cl else {}

            if p == "/api/mc/start":
                if mc_process and mc_process.poll() is None:
                    self._send(200, {"ok": False, "msg": "Zaten çalışıyor"})
                else:
                    ok, msg = _do_start()
                    self._send(200, {"ok": ok, "msg": msg})

            elif p == "/api/mc/stop":
                if mc_process and mc_process.poll() is None:
                    server_state["status"] = "stopping"
                    if body.get("force"):
                        mc_process.kill()
                    else:
                        try:
                            mc_process.stdin.write(b"save\nstop\n")
                            mc_process.stdin.flush()
                        except Exception: pass
                    self._send(200, {"ok": True, "msg": "Durduruluyor"})
                else:
                    self._send(200, {"ok": False, "msg": "Çalışmıyor"})

            elif p == "/api/mc/command":
                cmd = body.get("cmd", "")
                if cmd and mc_process and mc_process.poll() is None:
                    try:
                        mc_process.stdin.write(f"{cmd}\n".encode())
                        mc_process.stdin.flush()
                        self._send(200, {"ok": True})
                    except Exception as e:
                        self._send(200, {"ok": False, "error": str(e)})
                else:
                    self._send(200, {"ok": False, "msg": "MC çalışmıyor"})

            elif p in ("/api/agent/register", "/api/agent/heartbeat"):
                nid    = body.get("node_id", "")
                tunnel = body.get("tunnel", "")
                if nid and tunnel:
                    try: vcluster.register_agent(tunnel, nid, body)
                    except Exception: pass
                self._send(200, {"ok": True})

            elif p == "/api/internal/status_msg":
                msg = body.get("msg", "")
                if msg: print(f"[STATUS] {msg}", flush=True)
                self._send(200, {"ok": True})

            else:
                self._send(404, {"ok": False, "error": "not found"})

    # Agent heartbeat thread (vcluster için)
    threading.Thread(target=_pool_health_watchdog, daemon=True).start()
    threading.Thread(target=_pool_auto_optimize,   daemon=True).start()

    # Otomatik MC başlat
    def _auto_start():
        time.sleep(3)
        _mc_log("[MC_ONLY] ⚡ Minimal API hazır, MC başlatılıyor...")
        _do_start()
    threading.Thread(target=_auto_start, daemon=True).start()

    print(f"[MC_ONLY] ⚡ HTTP API :{PANEL_PORT} (Flask yok → Xmx=370MB)", flush=True)
    HTTPServer(("0.0.0.0", PANEL_PORT), _Handler).serve_forever()


# ══════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
#  İKİ FAZLI BAŞLATMA
#  Faz 1: Minimal HTTP (5MB) + MC Xmx=340MB  bootstrap ~475MB ✓
#  Faz 2: MC "Done!" sonrası Flask+SocketIO devralır (swap karşılar)
# ══════════════════════════════════════════════════════════════


def _minimal_http_phase():
    """
    Faz 1: stdlib HTTPServer — Flask/SocketIO/eventlet yok.
    ~80MB tasarruf → Xmx=340MB.
    MC başlatır, Done! gelince Flask moduna geçer.
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse as _up2

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _j(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = _up2.urlparse(self.path).path
            if p in ("/", "/health", "/api/ping"):
                self._j(200, {"ok": True, "phase": 1,
                              "status": server_state.get("status", "stopped")})
            else:
                self._j(200, {"ok": True, "phase": 1,
                              "status": server_state.get("status", "stopped")})

        def do_POST(self):
            global mc_process
            p  = _up2.urlparse(self.path).path
            cl = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(cl)) if cl else {}
            except Exception:
                body = {}

            if p in ("/api/start", "/api/mc/start"):
                if mc_process and mc_process.poll() is None:
                    self._j(200, {"ok": False, "msg": "Zaten çalışıyor"})
                else:
                    ok, msg = start_server()
                    self._j(200, {"ok": ok, "msg": msg})

            elif p in ("/api/agent/register", "/api/agent/heartbeat"):
                nid    = body.get("node_id", "")
                tunnel = body.get("tunnel", "")
                if nid and tunnel:
                    try: vcluster.register_agent(tunnel, nid, body)
                    except Exception: pass
                self._j(200, {"ok": True})

            elif p == "/api/internal/status_msg":
                msg = body.get("msg", "")
                if msg:
                    print(f"[STATUS] {msg}", flush=True)
                    console_buf.append({"ts": datetime.now().strftime("%H:%M:%S"),
                                        "line": msg})
                self._j(200, {"ok": True})

            elif p == "/api/internal/tunnel":
                tunnel_info.update(body)
                self._j(200, {"ok": True})

            else:
                self._j(200, {"ok": True})

    srv = HTTPServer(("0.0.0.0", PANEL_PORT), _H)
    srv.timeout = 0.5

    print(f"[Phase1] ⚡ Minimal HTTP :{PANEL_PORT} aktif (Flask yok Xmx=340MB)", flush=True)

    # Otomatik MC başlat
    def _auto_mc():
        time.sleep(2)
        print("[Phase1] 🚀 MC otomatik başlatılıyor...", flush=True)
        start_server()
    threading.Thread(target=_auto_mc, daemon=True).start()

    # MC Done! gelene kadar minimal HTTP sun
    while not _bootstrap_done.is_set():
        try:
            srv.handle_request()
        except Exception:
            pass

    # Done! → Flask'a geçiş
    print("[Phase1→2] ✅ MC hazır — Flask+SocketIO başlatılıyor...", flush=True)
    try:
        srv.server_close()
    except Exception:
        pass

    # Bellek temizle
    try: open("/proc/sys/vm/drop_caches", "w").write("1")
    except Exception: pass

    # Daemon thread'leri başlat
    threading.Thread(target=_ram_monitor,          daemon=True).start()
    threading.Thread(target=_ram_watchdog,         daemon=True).start()
    threading.Thread(target=_pool_health_watchdog, daemon=True).start()
    threading.Thread(target=_pool_auto_optimize,   daemon=True).start()
    threading.Thread(target=_region_disk_daemon,   daemon=True).start()
    threading.Thread(target=_region_cache_daemon,  daemon=True).start()
    _register_cluster_blueprint()

    print(f"[Phase2] 🚀 Flask+SocketIO :{PANEL_PORT} başlatılıyor...", flush=True)
    socketio.run(app, host="0.0.0.0", port=PANEL_PORT,
                 debug=False, use_reloader=False, log_output=False)


def _register_cluster_blueprint():
    """
    Phase1→Phase2 geçişinde çağrılır.
    cluster_api blueprint zaten modül yüklenirken register edildi
    (app.register_blueprint satırı). Bu fonksiyon ek thread'leri
    başlatmak ve emit yapmak için kullanılır.
    """
    try:
        # vcluster sağlık döngüsü zaten cluster.py'de başlıyor,
        # burada panel log'una emit yapalım.
        socketio.emit("pool_update", vcluster.summary())
        log("[Panel] ✅ Phase2: Cluster API + Kaynak Havuzu aktif")
    except Exception as e:
        log(f"[Panel] ⚠️  Blueprint kayıt: {e}")


if __name__ == "__main__":
    MC_DIR.mkdir(parents=True, exist_ok=True)
    if MC_ONLY:
        _run_mc_only()
    else:
        # Cuberite C++ ~50MB — iki fazlı başlatmaya gerek yok, direkt Flask
        threading.Thread(target=_ram_monitor,          daemon=True).start()
        threading.Thread(target=_ram_watchdog,         daemon=True).start()
        threading.Thread(target=_pool_health_watchdog, daemon=True).start()
        threading.Thread(target=_pool_auto_optimize,   daemon=True).start()
        threading.Thread(target=_region_disk_daemon,   daemon=True).start()
        threading.Thread(target=_region_cache_daemon,  daemon=True).start()
        def _auto_start_mc():
            import time as _t; _t.sleep(3)
            try: start_server()
            except Exception as e:
                print(f"[Panel] ⚠️  MC başlatma hatası: {e}", flush=True)
        threading.Thread(target=_auto_start_mc, daemon=True).start()
        print(f"[Panel] 🚀 Flask+SocketIO :{PANEL_PORT}", flush=True)
        socketio.run(app, host="0.0.0.0", port=PANEL_PORT,
                     debug=False, use_reloader=False, log_output=False)
