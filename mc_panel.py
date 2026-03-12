"""
⛏️  Minecraft Yönetim Paneli — v10.0
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

from flask import Flask, request, jsonify, send_file, abort, Response
from flask_socketio import SocketIO, emit

# ── Ayarlar ───────────────────────────────────────────────────
MC_DIR     = Path("/minecraft")
MC_JAR     = MC_DIR / "server.jar"
MC_PORT    = 25565
PANEL_PORT = int(os.environ.get("PORT", "5000"))
MC_VERSION = "1.21.1"
MC_RAM     = os.environ.get("MC_RAM", "2G")

# ── Render plan limitleri (psutil host değil, tahsis edilen kapasite) ─
RENDER_RAM_LIMIT_MB  = int(os.environ.get("CONTAINER_RAM_MB",    "512"))
RENDER_DISK_LIMIT_GB = float(os.environ.get("RENDER_DISK_LIMIT_GB", "18.0"))

# ── Global durum ─────────────────────────────────────────────
mc_process   = None
console_buf  = deque(maxlen=3000)
players      = {}
tunnel_info  = {"url": "", "host": ""}
server_state = {
    "status": "stopped", "tps": 20.0, "tps15": 20.0, "tps5": 20.0,
    "ram_mb": 0, "uptime": 0, "started": None,
    "version": "—", "max_players": 20, "online_players": 0,
}

# ── Resource Pool ─────────────────────────────────────────────
# resource_pool.py'deki ResourcePool singleton kullanılır.
# _agents/_agents_lock KALDIRILDI → _pool.agents/_pool.lock kullanılıyor.
from resource_pool import pool as _pool   # AgentClient + health monitor burada

# ── Userspace Swap (LD_PRELOAD) ───────────────────────────────────
USERSWAP_SO  = "/usr/local/lib/userswap.so"
USERSWAP_SRC = "/app/userswap.c"

_USERSWAP_C = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
#include <errno.h>
#include <stdint.h>
#include <pthread.h>

#define SWAP_FILE     "/swapfile_mmap"
#define SWAP_SIZE     (4L*1024L*1024L*1024L)
#define MIN_INTERCEPT (256L*1024)

static int             swap_fd  = -1;
static off_t           swap_pos = 0;
static pthread_mutex_t swap_mx  = PTHREAD_MUTEX_INITIALIZER;
static void* (*real_mmap)(void*,size_t,int,int,int,off_t) = NULL;
static volatile long stat_ok=0,stat_mb=0,stat_fb=0;

__attribute__((constructor))
static void userswap_init(void){
    real_mmap=dlsym(RTLD_NEXT,"mmap");
    if(!real_mmap)return;
    struct stat st;
    if(stat(SWAP_FILE,&st)==0&&st.st_size>=SWAP_SIZE){
        swap_fd=open(SWAP_FILE,O_RDWR);
        if(swap_fd>=0){fprintf(stderr,"[UserSwap] Mevcut 4GB swap kullaniliyor
");return;}
    }
    swap_fd=open(SWAP_FILE,O_RDWR|O_CREAT|O_TRUNC,0600);
    if(swap_fd<0){fprintf(stderr,"[UserSwap] open errno=%d
",errno);return;}
    fprintf(stderr,"[UserSwap] 4GB dosya swap olusturuluyor...
");
    if(posix_fallocate(swap_fd,0,SWAP_SIZE)!=0)(void)ftruncate(swap_fd,SWAP_SIZE);
    fprintf(stderr,"[UserSwap] Userspace Swap hazir (4GB file-backed)
");
}

__attribute__((destructor))
static void userswap_fini(void){
    if(swap_fd>=0){
        fprintf(stderr,"[UserSwap] %ld intercept %ldMB %ld fb
",stat_ok,stat_mb,stat_fb);
        close(swap_fd);
    }
}

void* mmap(void *addr,size_t length,int prot,int flags,int fd,off_t offset){
    if(swap_fd>=0&&(flags&MAP_ANONYMOUS)&&!(flags&MAP_FIXED)
       &&length>=(size_t)MIN_INTERCEPT&&(prot&(PROT_READ|PROT_WRITE))){
        size_t aligned=(length+4095UL)&~4095UL;
        off_t swap_off;
        pthread_mutex_lock(&swap_mx);
        int ok=(swap_pos+(off_t)aligned<=SWAP_SIZE);
        if(ok){swap_off=swap_pos;swap_pos+=(off_t)aligned;}
        pthread_mutex_unlock(&swap_mx);
        if(ok){
            int nf=(flags&~MAP_ANONYMOUS&~MAP_PRIVATE)|MAP_SHARED;
            void*p=real_mmap(addr,length,prot,nf,swap_fd,swap_off);
            if(p!=MAP_FAILED){
                __sync_fetch_and_add(&stat_ok,1L);
                __sync_fetch_and_add(&stat_mb,(long)(length>>20));
                return p;
            }
            pthread_mutex_lock(&swap_mx);
            if(swap_pos==swap_off+(off_t)aligned)swap_pos=swap_off;
            pthread_mutex_unlock(&swap_mx);
            __sync_fetch_and_add(&stat_fb,1L);
        }
    }
    return real_mmap(addr,length,prot,flags,fd,offset);
}
"""


def _build_userswap():
    """
    userswap.so derlenmemişse derle.
    LD_PRELOAD ile JVM'in anonim mmap'lerini dosya destekli yapar → Userspace Swap.
    """
    import shutil
    if Path(USERSWAP_SO).exists():
        return True
    if not shutil.which("gcc"):
        # gcc yok → apt ile kur
        r = subprocess.run("apt-get install -y gcc 2>/dev/null", shell=True, capture_output=True)
        if not shutil.which("gcc"):
            log("[UserSwap] ⚠️  gcc bulunamadı — userswap devre dışı")
            return False
    try:
        src_path = Path("/tmp/userswap.c")
        src_path.write_text(_USERSWAP_C)
        r = subprocess.run(
            f"gcc -O2 -shared -fPIC -o {USERSWAP_SO} {src_path} -ldl -lpthread",
            shell=True, capture_output=True
        )
        if r.returncode == 0:
            log(f"[UserSwap] ✅ userswap.so derlendi → {USERSWAP_SO}")
            return True
        else:
            log(f"[UserSwap] ⚠️  Derleme hatası: {r.stderr.decode()[:120]}")
            return False
    except Exception as e:
        log(f"[UserSwap] ⚠️  {e}")
        return False

app = Flask(__name__)
app.config["SECRET_KEY"] = "mc-panel-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet",
                    ping_timeout=60, ping_interval=25)


# ══════════════════════════════════════════════════════════════
#  YARDIMCI FONKSİYONLAR
# ══════════════════════════════════════════════════════════════

def log(line: str):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "line": line.rstrip()}
    console_buf.append(entry)
    socketio.emit("console_line", entry)
    _parse_mc_output(line)


def _parse_mc_output(line: str):
    m = re.search(r"(\w+)\[/.+\] logged in", line)
    if m:
        players[m.group(1)] = {"op": False, "joined_ts": time.time()}
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list())
        socketio.emit("stats_update", server_state)
        return
    m = re.search(r"(\w+) lost connection|(\w+) left the game", line)
    if m:
        players.pop(m.group(1) or m.group(2), None)
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list())
        socketio.emit("stats_update", server_state)
        return
    m = re.search(r"TPS from last 1m, 5m, 15m: ([\d.]+),\s*([\d.]+),\s*([\d.]+)", line)
    if m:
        server_state["tps"]  = float(m.group(1))
        server_state["tps5"] = float(m.group(2))
        server_state["tps15"]= float(m.group(3))
        socketio.emit("stats_update", server_state)
        return
    m = re.search(r"Starting minecraft server version (.+)", line)
    if m:
        server_state["version"] = m.group(1).strip()
    if "Done" in line and "help" in line.lower():
        server_state["status"]  = "running"
        server_state["started"] = time.time()
        server_state["online_players"] = 0
        socketio.emit("server_status", server_state)
        log("[Panel] ✅ Minecraft Server hazır!")
        threading.Thread(target=_tps_monitor, daemon=True).start()
    if "Stopping server" in line:
        server_state["status"] = "stopping"
        socketio.emit("server_status", server_state)


def _players_list():
    return [{"name": n, **info} for n, info in players.items()]


def _tps_monitor():
    while mc_process and mc_process.poll() is None:
        time.sleep(30)
        if server_state["status"] == "running":
            send_command("tps")


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
#  RESOURCE POOL — Agent yönetimi  (resource_pool.py'e delege edilir)
# ══════════════════════════════════════════════════════════════
# Tüm agent kaydı, heartbeat, sağlık izleme ve dosya işlemleri
# resource_pool.ResourcePool (singleton: _pool) tarafından yönetilir.
# mc_panel.py yalnızca Flask route'larını barındırır.

def _pool_summary() -> dict:
    """resource_pool.pool.summary() biçimini mc_panel uyumlu hale getirir."""
    s = _pool.summary()
    res = s.get("resources", {})
    return {
        "total":   s["total"],
        "healthy": s["healthy"],
        "resources": {
            "ram_free_mb":   res.get("ram_free_mb",  0),
            "disk_free_gb":  round(res.get("disk_free_gb", 0), 1),
            "cache_used_mb": res.get("ram_cache_mb", 0),
            "cpu_cores":     res.get("cpu_cores",    0),
        },
        "agents": [
            {
                "node_id":      a["node_id"],
                "url":          a["url"],
                "healthy":      a["healthy"],
                "connected_at": a.get("last_ok", 0),
                "last_ping":    a.get("last_ok", 0),
                "ram":          a.get("ram",   {}),
                "disk":         a.get("disk",  {}),
                "cpu":          a.get("cpu",   {}),
                "proxy":        a.get("proxy", {}),
            }
            for a in s["agents"]
        ],
    }


def _auto_archive_old_regions(older_than_days: int = 0):
    """
    Ana sunucudaki eski region dosyalarını en fazla boş diski olan
    agent'a yükler. Ardından açılan disk alanını swap'a dönüştürür.
    resource_pool._most_disk() + store_region() kullanır.
    """
    import shutil as _sh
    best_agent = _pool._most_disk()
    if not best_agent:
        return 0, 0.0
    # Disk boş olsa bile arşivle — Agent diskini kullanmak Swap için yer açar

    archived = 0
    freed_mb = 0
    now      = time.time()

    for dim_dir in [MC_DIR / "world" / "region",
                    MC_DIR / "world_nether" / "DIM-1" / "region",
                    MC_DIR / "world_the_end" / "DIM1" / "region"]:
        if not dim_dir.exists():
            continue
        dim = dim_dir.parts[-3]

        # ÖNEMLİ: shutil.disk_usage("/") HOST diskini okur (örn: 68GB).
        # Render 18GB limiti cgroup ile uygulanır — gerçek kullanıma bakıyoruz.
        # Hesap: agent diskleri zaten bu iş için var, her zaman arşivle.
        import shutil as _shu2
        # Render limitini baz al (18GB) - ana sunucu gerçek kullanımı
        render_limit_gb  = float(os.environ.get("RENDER_DISK_LIMIT_GB", "18.0"))
        # /minecraft altındaki dosyaları say
        try:
            mc_used_gb = sum(
                f.stat().st_size for f in MC_DIR.rglob("*") if f.is_file()
            ) / 1e9
        except:
            mc_used_gb = 0.0
        # OS + Python + image tabanı ~4GB
        total_used_gb = 4.0 + mc_used_gb
        force_all = total_used_gb > (render_limit_gb * 0.7)  # %70 dolu → zorla arşivle

        for rf in sorted(dim_dir.glob("*.mca"), key=lambda f: f.stat().st_mtime):
            age_days = (now - rf.stat().st_mtime) / 86400
            if not force_all and age_days < older_than_days:
                continue
            if _pool.region_exists_remote(dim, rf.name):
                continue   # Zaten uzakta
            try:
                ok = _pool.store_region(dim, rf)
                if ok:
                    freed_mb += rf.stat().st_size / 1e6
                    rf.unlink()
                    archived += 1
            except Exception:
                continue

    if archived > 0:
        new_free_gb = _sh.disk_usage("/").free / 1e9
        log(f"[Pool] 💾 {archived} region arşivlendi ({freed_mb:.0f}MB) → Ana disk:{new_free_gb:.1f}GB boş")

    return archived, freed_mb



def _world_backup_loop():
    """
    Her 3 dakikada MC world/*.mca dosyalarını agent diskine YEDEK olarak gönder.
    Silmez — sadece kopyalar. Agent'ların 140GB Disk Deposunu doldurur.
    """
    import hashlib as _hlib
    _sent_hashes: dict = {}   # path → md5 — sadece değişenleri gönder

    def _file_md5(path):
        try:
            return _hlib.md5(open(path, "rb").read(256*1024)).hexdigest()
        except: return ""

    # İlk bekleme: MC + agent'lar hazır olsun
    time.sleep(60)

    while True:
        try:
            if not (mc_process and mc_process.poll() is None):
                time.sleep(30); continue
            if _pool.agent_count() == 0:
                time.sleep(30); continue

            sent = 0
            for dim_dir in [
                MC_DIR / "world" / "region",
                MC_DIR / "world_nether" / "DIM-1" / "region",
                MC_DIR / "world_the_end" / "DIM1" / "region",
            ]:
                if not dim_dir.exists():
                    continue
                dim = dim_dir.parts[-3]
                for rf in dim_dir.glob("*.mca"):
                    try:
                        md5 = _file_md5(rf)
                        cache_key = f"{dim}/{rf.name}"
                        if _sent_hashes.get(cache_key) == md5:
                            continue   # Değişmemiş — atla
                        ok = _pool.store_region_backup(dim, rf)
                        if ok:
                            _sent_hashes[cache_key] = md5
                            sent += 1
                    except Exception:
                        continue

            if sent:
                log(f"[Pool] 💾 Dünya yedeklendi: {sent} region → agent disk")
                socketio.emit("pool_update", _pool_summary())
        except Exception as e:
            log(f"[Pool] ⚠️  Yedek hatası: {e}")
        time.sleep(180)   # 3 dakika


# ── Agent başına kaç MB cache dolduralım ──────────────────────────────────
# Her agent 382MB free RAM var, RamCache limiti agent'ta RAM_CACHE_MB env ile ayarlanır.
# Ana sunucu bu değeri bilmez — agent'ın cache/stats endpoint'inden okur.
# Güvenli hedef: cache limitinin %90'ı
AGENT_CACHE_FILL_TARGET = 0.97   # Her agenti limitle doldurmaya çalış


def _get_agent_cache_limit_mb(agent_client) -> int:
    """Agent'ın gerçek RAM_CACHE_MB limitini öğren (API'den)."""
    try:
        stats = agent_client.cache_stats()
        return stats.get("limit_mb", 256)
    except:
        return 256


def _warm_single_agent(agent_client, log_fn=None, fill_regions: bool = True):
    """
    Tek bir agent'ı cache limitinin %90'ına kadar doldur:
      1. server.jar   (~47MB)
      2. Config dosyaları (~1MB)
      3. Plugin JARlar  (değişken)
      4. World region dosyaları (.mca) — ASIL DOLDURMA KAYNAGI
         Her agent farklı region'ları alır → tüm harita dağıtılmış şekilde önbelleğe girer.

    5 agent × 256MB = 1280MB toplam cache kapasitesi.
    """
    _log = log_fn or print
    pushed_bytes = 0
    pushed_count = 0

    limit_mb    = _get_agent_cache_limit_mb(agent_client)
    target_mb   = int(limit_mb * AGENT_CACHE_FILL_TARGET)
    target_bytes= target_mb * 1024 * 1024

    def _send(key: str, data: bytes) -> bool:
        nonlocal pushed_bytes, pushed_count
        if pushed_bytes >= target_bytes:
            return False  # Bu agent dolu
        try:
            ok = agent_client.cache_set(key, data)
            if ok:
                pushed_bytes += len(data)
                pushed_count += 1
            return ok
        except:
            return False

    # 1. Server JAR (tüm agentlara — sık erişilen)
    if MC_JAR.exists():
        try:
            _send("mc/server.jar", MC_JAR.read_bytes())
        except: pass

    # 2. Config dosyaları
    for cfg in ["paper.yml", "spigot.yml", "bukkit.yml", "server.properties",
                "paper-world-defaults.yml", "config/paper-global.yml"]:
        p = MC_DIR / cfg
        if p.exists():
            try: _send(f"mc/config/{cfg}", p.read_bytes())
            except: pass

    # 3. Plugin JARlar
    plugins_dir = MC_DIR / "plugins"
    if plugins_dir.exists():
        for pjar in sorted(plugins_dir.glob("*.jar"))[:20]:
            try: _send(f"mc/plugins/{pjar.name}", pjar.read_bytes())
            except: pass

    # 4. World region dosyaları — TÜM AGENT'LARA KOPYALA (round-robin DEĞİL)
    # ─────────────────────────────────────────────────────────────────────
    # Round-robin: 8 agent → her agent 1/8 bölge → küçük dünyada ~5MB/agent
    # YENİ: Her agent tüm bölgeleri alır (400MB limitine kadar)
    # 8 agent × 400MB = 3.2GB toplam → herhangi bir agent herhangi isteği karşılar
    # Yedeklilik: 1 agent düşse diğer 7'si full cache'le devam eder
    if fill_regions and pushed_bytes < target_bytes:
        dim_dirs = [
            (MC_DIR / "world"          / "region",          "world"),
            (MC_DIR / "world_nether"   / "DIM-1" / "region","world_nether"),
            (MC_DIR / "world_the_end"  / "DIM1"  / "region","world_the_end"),
        ]
        for region_dir, dim_name in dim_dirs:
            if not region_dir.exists():
                continue
            # En son erişilen (aktif) region'lar önce
            mca_files = sorted(region_dir.glob("*.mca"),
                               key=lambda f: f.stat().st_mtime, reverse=True)
            for rf in mca_files:
                if pushed_bytes >= target_bytes:
                    break
                key = f"mc/region/{dim_name}/{rf.name}"
                try:
                    _send(key, rf.read_bytes())  # Her agent bu dosyayı alır
                except: pass

    # 5. Entities / poi / data alt dizinleri (kalan alan varsa)
    if pushed_bytes < target_bytes:
        for sub in ["entities", "poi"]:
            sub_dir = MC_DIR / "world" / sub
            if not sub_dir.exists():
                continue
            for sf in sorted(sub_dir.rglob("*.mca"),
                             key=lambda f: f.stat().st_mtime, reverse=True):
                if pushed_bytes >= target_bytes:
                    break
                try:
                    if sf.stat().st_size <= 8 * 1024 * 1024:
                        _send(f"mc/{sub}/{sf.name}", sf.read_bytes())
                except: pass

    used_mb = pushed_bytes // 1024 // 1024
    if pushed_count:
        _log(f"[Pool] 🧠 {agent_client.node_id}: {pushed_count} dosya, "
             f"{used_mb}MB/{target_mb}MB hedef → cache doldu")
    return pushed_count


def _ram_cache_warm_loop():
    """
    Tüm agent cache'lerini paralel doldur.
    Her 5 dakikada bir yeni region'ları ve yeni agentları güncelle.
    """
    # MC JAR + en az 1 agent hazır olana kadar bekle (max 15 dk)
    for _ in range(900):
        if MC_JAR.exists() and _pool.agent_count() > 0:
            break
        time.sleep(1)
    else:
        log("[Pool] ⚠️  Cache warm: JAR veya agent 15dk içinde hazır olmadı")
        return

    time.sleep(10)  # MC başlasın + world dosyaları oluşsun

    def _fill_all_agents():
        agents = _pool.get_agents()
        if not agents:
            return 0
        threads, results = [], []

        def _t(a):
            n = _warm_single_agent(a, log_fn=log, fill_regions=True)
            results.append(n)

        for a in agents:
            t = threading.Thread(target=_t, args=(a,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=180)
        total = sum(results)
        if total:
            log(f"[Pool] 🧠 Cache tamamlandı: {len(agents)} agent, toplam {total} dosya gönderildi")
            socketio.emit("pool_update", _pool_summary())
        return total

    _fill_all_agents()

    # Periyodik: yeni agentları ısıt + tüm havuzu tazele
    _warmed_agents: set = set(a.node_id for a in _pool.get_agents())
    _last_fill_time: float = time.time()

    while True:
        time.sleep(120)  # 2dk kontrol (yeni region'lar hızlı cache'e girer)
        try:
            if _pool.agent_count() == 0:
                continue

            current = set(a.node_id for a in _pool.get_agents())
            new_ids = current - _warmed_agents
            if new_ids:
                log(f"[Pool] 🧠 Yeni agent ısınıyor: {new_ids}")
                for nid in new_ids:
                    a = _pool.agents.get(nid)
                    if a:
                        try:
                            _warm_single_agent(a, log_fn=log, fill_regions=True)
                            import gc as _gcn; _gcn.collect()
                            time.sleep(2)
                        except Exception as _ew:
                            log(f"[Pool] ⚠️  Yeni agent warm {nid}: {_ew}")
            _warmed_agents.update(current)

            # 5dk'da bir tüm havuzu tazele (oyun büyüdükçe yeni region'lar eklenir)
            if time.time() - _last_fill_time >= 300:
                _fill_all_agents()
                _last_fill_time = time.time()
        except Exception as e:
            log(f"[Pool] ⚠️  Cache warm döngü hatası: {e}")


def _pool_auto_optimize():
    """
    1) Agent gelene kadar bekle (maks 90sn)
    2) Hemen swap kur (modprobe loop + losetup --find --show)
    3) Region arşivle
    4) Her 300sn tekrar
    """
    for _ in range(90):
        if _pool.agent_count() > 0:
            log(f"[Pool] ✅ {_pool.agent_count()} agent hazır → Swap + arşiv başlıyor")
            break
        time.sleep(1)

    # Render.com'da swapon çalışmaz (EPERM) — swap denemesi atlandı
    log("[Pool] ℹ️  Render: swapon izin verilmiyor → swap yok, overcommit aktif")

    # Render limiti tabanlı disk kontrolü (host FS değil)
    render_limit_gb = float(os.environ.get("RENDER_DISK_LIMIT_GB", "18.0"))

    def _render_disk_used_gb():
        """Gerçek Render disk kullanımı: /minecraft + swap dosyaları."""
        mc_gb = 0.0
        try:
            mc_gb = sum(
                f.stat().st_size for f in MC_DIR.rglob("*") if f.is_file()
            ) / 1e9
        except: pass
        swap_gb = sum(
            os.path.getsize(f) for f in ["/swapfile", "/swapfile_mmap"]
            if os.path.exists(f)
        ) / 1e9
        return 4.0 + mc_gb + swap_gb  # 4GB OS/image tabanı

    # İlk arşiv
    try:
        used_gb = _render_disk_used_gb()
        # Disk %70+ dolu = acil (days=0), değilse 3 günden eski regionları arşivle
        # (7 gün çok uzun — agentlar boşta kalır)
        days = 0 if used_gb > render_limit_gb * 0.70 else 3
        log(f"[Pool] 📊 Render disk: ~{used_gb:.1f}GB / {render_limit_gb}GB → eşik:{days}gün")
        result = _auto_archive_old_regions(older_than_days=days)
        if result and result[0]:
            log(f"[Pool] 📦 İlk arşiv: {result[0]} region, {result[1]:.0f}MB (≥{days}gün)")
        else:
            log(f"[Pool] ℹ️  Arşiv: region yok veya hepsi yeni (eşik:{days}gün, disk:{used_gb:.1f}GB)")
        socketio.emit("pool_update", _pool_summary())
    except Exception as e:
        log(f"[Pool] ⚠️  İlk arşiv hatası: {e}")

    while True:
        time.sleep(180)  # 3 dakika (300 yerine — daha agresif)
        try:
            used_gb2 = _render_disk_used_gb()
            days2    = 0 if used_gb2 > render_limit_gb * 0.70 else 3
            _auto_archive_old_regions(older_than_days=days2)
            socketio.emit("pool_update", _pool_summary())
        except Exception as e:
            log(f"[Pool] ⚠️  Arşiv hatası: {e}")


# ══════════════════════════════════════════════════════════════
#  MC SERVER YÖNETİMİ
# ══════════════════════════════════════════════════════════════

def download_paper():
    import ssl
    ctx = ssl.create_default_context()
    log("[Panel] 📥 Paper MC indiriliyor...")
    try:
        api_url = f"https://api.papermc.io/v2/projects/paper/versions/{MC_VERSION}/builds"
        req = _urllib_req.Request(api_url, headers={"User-Agent": "MCPanel/10.0"})
        with _urllib_req.urlopen(req, timeout=20, context=ctx) as r:
            builds = json.loads(r.read()).get("builds", [])
        if not builds:
            raise ValueError("Build listesi boş")
        build    = builds[-1]["build"]
        jar_name = f"paper-{MC_VERSION}-{build}.jar"
        url = (f"https://api.papermc.io/v2/projects/paper"
               f"/versions/{MC_VERSION}/builds/{build}/downloads/{jar_name}")
        log(f"[Panel] 📦 {jar_name} (build #{build})...")
        req2 = _urllib_req.Request(url, headers={"User-Agent": "MCPanel/10.0"})
        done = 0
        with _urllib_req.urlopen(req2, timeout=180, context=ctx) as r2:
            total = int(r2.headers.get("Content-Length", 0))
            with open(MC_JAR, "wb") as f:
                while True:
                    chunk = r2.read(65536)
                    if not chunk: break
                    f.write(chunk); done += len(chunk)
                    if total:
                        socketio.emit("download_progress",
                                      {"pct": int(done*100/total), "done": done, "total": total})
        log(f"[Panel] ✅ Paper MC {MC_VERSION} build #{build} indirildi ({done//1024//1024}MB)")
        return True
    except Exception as e:
        log(f"[Panel] ❌ İndirme hatası: {e}")
        return False


def write_server_config():
    (MC_DIR / "eula.txt").write_text("eula=true\n")
    props = MC_DIR / "server.properties"
    props.write_text(
        f"server-port={MC_PORT}\nmax-players=20\nonline-mode=false\n"
        "gamemode=survival\ndifficulty=normal\nlevel-name=world\n"
        "motd=\\u00A7a\\u00A7lRender MC Server\n"
        "view-distance=4\nsimulation-distance=3\n"
        "spawn-protection=0\nallow-flight=true\n"
        "enable-rcon=false\nmax-tick-time=60000\nwhite-list=false\n"
        "enable-command-block=true\npvp=true\ngenerate-structures=true\n"
        "allow-nether=true\nsync-chunk-writes=false\n"
        "entity-broadcast-range-percentage=50\n"
    )
    config = MC_DIR / "config"
    config.mkdir(exist_ok=True)
    pw = config / "paper-world-defaults.yml"
    pw.write_text(
        "world-settings:\n  default:\n"
        "    spawn-limits:\n      monsters: 40\n      animals: 8\n"
        "      water-animals: 3\n      water-ambient: 10\n"
        "    chunks:\n      auto-save-interval: 12000\n"
        "    max-auto-save-chunks-per-tick: 4\n"
        "    prevent-moving-into-unloaded-chunks: true\n"
    )


# _loop_swap_panel KALDIRILDI: Render.com'da swapon izni yok (EPERM)


def get_jvm_args():
    import psutil as _ps
    container_ram_mb = int(os.environ.get("CONTAINER_RAM_MB", "512"))
    for path in ["/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
        try:
            val = open(path).read().strip()
            if val not in ("max", "-1"):
                mb = int(val) // 1024 // 1024
                if 64 < mb < 65536:
                    container_ram_mb = mb; break
        except: pass

    agent_count = _pool.agent_count()

    # Render.com free tier: 512MB container, swapon yok AMA UserSwap aktif.
    # UserSwap (LD_PRELOAD mmap hook) → JVM anonim mmap'leri /swapfile_mmap'e yönlendirir.
    # Fiziksel taşma dosyaya gider → SIGKILL yok, OOM yok.
    #
    # Paper 1.21 Bootstrap blok-state yüklemesi: ~260MB heap gerektirir.
    # Xmx=220 → OOM: Java heap space (Bootstrap sırasında crash!)
    #
    # Güvenli hesap (UserSwap ile):
    #   Fiziksel: Xmx_rss + Meta(96) + Code(28) + Class(24) + Stack(14) + Python(130) ≤ 512
    #   Xmx=320 → RSS_JVM ≈ 250MB (aktif heap) + 220MB (swap'a taşan) → fiziksel ~480MB peak
    #   Peak 512MB'yi aşınca UserSwap devreye girer → dosyaya yazar, crash YOK
    #   Steady-state (GC sonrası): RSS ~380MB → güvenli
    xmx_mb = 320   # UserSwap aktif → Paper 1.21 bootstrap için yeterli heap
    xms_mb = 48    # Düşük başlangıç → JVM lazy expand eder

    userswap_ok = os.path.exists(USERSWAP_SO)
    swap_label  = f"UserSwap(4GB)" if userswap_ok else "NoSwap"
    log(f"[Panel] 🧠 Container={container_ram_mb}MB {swap_label} Agents={agent_count} → Xms={xms_mb}M Xmx={xmx_mb}M")

    java_cmd = []
    if userswap_ok:
        java_cmd = ["env", f"LD_PRELOAD={USERSWAP_SO}"]
    else:
        log("[Panel] ⚠️  userswap.so yok — LD_PRELOAD atlandı (OOM riski artabilir)")
    java_cmd.append("java")

    return java_cmd + [
        f"-Xms{xms_mb}M", f"-Xmx{xmx_mb}M",
        # ── Bellek alanları (UserSwap ile taşma dosyaya gider) ──
        # Meta + Code + Class + Stack ≈ 162MB sabit
        # Fiziksel peak ≈ 320(heap) + 162(meta/etc) + 130(python) = 612MB → ~100MB UserSwap'a
        "-XX:MaxMetaspaceSize=96m",
        "-XX:CompressedClassSpaceSize=24m",
        "-XX:ReservedCodeCacheSize=28m",
        "-Xss256k",
        # ── DirectBuffer: SINIR YOK ──
        # Paper MC Netty için 32-128MB DirectBuffer gerekir.
        # MaxDirectMemorySize=32m CRASH yaratır (OOM: Cannot reserve direct buffer).
        # Sınır belirtilmezse JVM varsayılan = Xmx (220MB) olur → yeterli.
        # ── GC: G1 ──
        "-XX:+UseG1GC",
        "-XX:+ParallelRefProcEnabled",
        "-XX:MaxGCPauseMillis=100",
        "-XX:ConcGCThreads=1",
        "-XX:ParallelGCThreads=1",
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+DisableExplicitGC",
        "-XX:G1NewSizePercent=20",
        "-XX:G1MaxNewSizePercent=40",
        "-XX:G1HeapRegionSize=2m",
        "-XX:G1ReservePercent=10",
        "-XX:InitiatingHeapOccupancyPercent=15",
        "-XX:SoftRefLRUPolicyMSPerMB=0",
        "-XX:+UseStringDeduplication",
        "-XX:+UseCompressedOops",
        "-XX:+OptimizeStringConcat",
        # ── Diğer ──
        "-Djava.net.preferIPv4Stack=true",
        "-Dfile.encoding=UTF-8",
        "-Dcom.mojang.eula.agree=true",
        "-Xlog:disable",
        "-jar", str(MC_JAR), "--nogui",
    ]


def start_server():
    global mc_process
    if mc_process and mc_process.poll() is None:
        return False, "Server zaten çalışıyor"
    MC_DIR.mkdir(parents=True, exist_ok=True)
    # Userspace swap kütüphanesini derle (yoksa)
    _build_userswap()
    if not MC_JAR.exists():
        server_state["status"] = "downloading"
        socketio.emit("server_status", server_state)
        if not download_paper():
            server_state["status"] = "stopped"
            socketio.emit("server_status", server_state)
            return False, "Jar indirilemedi"
    write_server_config()
    server_state.update({"status": "starting", "online_players": 0})
    players.clear()
    socketio.emit("server_status", server_state)
    socketio.emit("players_update", [])
    jvm = get_jvm_args()
    log(f"[Panel] 🚀 Server başlatılıyor...")
    try:
        mc_process = subprocess.Popen(
            jvm, cwd=str(MC_DIR),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except Exception as e:
        log(f"[Panel] ❌ Başlatma hatası: {e}")
        server_state["status"] = "stopped"
        socketio.emit("server_status", server_state)
        return False, str(e)
    threading.Thread(target=_stdout_reader, daemon=True).start()
    # MC başlatılınca mevcut tüm agent'lara proxy aç
    def _start_all_proxies():
        # MC tünel URL'si hazır olana kadar bekle (max 5 dk)
        for _ in range(300):
            mc_host = tunnel_info.get("host", "")
            if mc_host:
                break
            time.sleep(1)
        else:
            log("[Pool] ⚠️  Proxy: 5dk içinde MC tüneli gelmedi — proxy atlandı")
            return

        agents = _pool.get_agents()
        if not agents:
            log("[Pool] ℹ️  Proxy: agent yok — proxy atlandı")
            return

        # Her agent kendi cloudflare tünelinden proxy kurar
        # → ana MC tünel host'una TCP relay (oyuncu yük dağıtımı)
        started = _pool.start_proxies(mc_host, mc_port=25565)
        if started:
            log(f"[Pool] 🔀 Proxy başlatıldı: {len(started)}/{len(agents)} agent → {mc_host}:25565")
        else:
            log(f"[Pool] ⚠️  Proxy başlatılamadı ({len(agents)} agent bağlı)")

    threading.Thread(target=_start_all_proxies, daemon=True).start()
    return True, "Başlatılıyor..."


def stop_server(force=False):
    global mc_process
    if not mc_process or mc_process.poll() is not None:
        return False, "Server çalışmıyor"
    server_state["status"] = "stopping"
    socketio.emit("server_status", server_state)
    if force:
        mc_process.kill()
    else:
        send_command("save-all"); time.sleep(1); send_command("stop")
    return True, "Durduruluyor..."


def send_command(cmd: str) -> bool:
    if mc_process and mc_process.poll() is None:
        try:
            mc_process.stdin.write(f"{cmd}\n".encode())
            mc_process.stdin.flush()
            return True
        except: pass
    return False


def _ram_watchdog():
    import psutil
    _pressure_count = 0
    while True:
        eventlet.sleep(8)
        try:
            mem = psutil.virtual_memory()
            swp = psutil.swap_memory()
            used_mb  = int(mem.used  / 1024 / 1024)
            total_mb = int(mem.total / 1024 / 1024)
            swap_pct = swp.percent if swp.total > 0 else 0

            # Render free = 512MB. Python ~150MB kullanıyor.
            # MC 280MB kullanıyorsa toplam ~430MB → %84 → uyar
            pressure = used_mb > (total_mb * 0.85) or swap_pct > 80

            if pressure:
                _pressure_count += 1
                try: open("/proc/sys/vm/drop_caches","w").write("3")
                except: pass

                if _pressure_count >= 2:
                    # Hafif MC temizliği
                    send_command("kill @e[type=item]")
                    send_command("kill @e[type=experience_orb]")

                if _pressure_count >= 3:
                    # Ağır baskı: kaydet + agent'a region offload tetikle
                    send_command("save-all")
                    log(f"[Panel] ⚠️  RAM Baskısı! Kullanılan={used_mb}MB/{total_mb}MB Swap=%{swap_pct:.0f}")
                    # Agent'a eski region'ları taşı
                    threading.Thread(
                        target=lambda: _auto_archive_old_regions(older_than_days=2),
                        daemon=True
                    ).start()
                    _pressure_count = 0
            else:
                _pressure_count = max(0, _pressure_count - 1)
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
                    "pool": _pool_summary()})


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
    new_host = d.get("host", "")
    old_host = tunnel_info.get("host", "")
    tunnel_info.update({"url": d.get("url",""), "host": new_host})
    socketio.emit("tunnel_update", tunnel_info)
    # Tünel yeni geldiyse proxy'leri güncelle
    if new_host and new_host != old_host and _pool.agent_count() > 0:
        def _update_proxies():
            time.sleep(2)
            _pool.stop_proxies()
            time.sleep(1)
            started = _pool.start_proxies(new_host, mc_port=25565)
            if started:
                log(f"[Pool] 🔀 Tünel güncellendi → {len(started)} proxy yeniden başlatıldı")
        threading.Thread(target=_update_proxies, daemon=True).start()
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
    _pool.set_logger(log)
    is_new = node_id not in _pool.agents
    _pool.register(tunnel_url, node_id, d)
    if is_new:
        log(f"[Pool] ✅ Yeni agent: {node_id} | "
            f"RAM:{d.get('ram',{}).get('free_mb',0)}MB | "
            f"Disk:{d.get('disk',{}).get('free_gb',0):.1f}GB | "
            f"CPU:{d.get('cpu',{}).get('cores',0)} core")
        # Yeni agent'ı hemen ısıt (warm loop'u bekleme)
        agent_client = _pool.agents.get(node_id)
        if agent_client and MC_JAR.exists():
            threading.Thread(
                target=_warm_single_agent,
                args=(agent_client, log),
                daemon=True
            ).start()
        # Proxy: MC tüneli varsa başlat
        mc_host = tunnel_info.get("host", "")
        if mc_host and mc_process and mc_process.poll() is None:
            def _new_agent_proxy():
                time.sleep(3)
                ok = agent_client.proxy_start(mc_host, 25565, 25565)
                if ok:
                    log(f"[Pool] 🔀 Yeni agent proxy: {node_id} → {mc_host}:25565")
            threading.Thread(target=_new_agent_proxy, daemon=True).start()
    socketio.emit("pool_update", _pool_summary())
    return jsonify({"ok": True})


@app.route("/api/agent/heartbeat", methods=["POST"])
def api_agent_heartbeat():
    d = request.json or {}
    if d.get("node_id") and d.get("tunnel"):
        _pool.set_logger(log)
        _pool.register(d["tunnel"], d["node_id"], d)
    socketio.emit("pool_update", _pool_summary())
    return jsonify({"ok": True})


@app.route("/api/pool/status")
def api_pool_status():
    return jsonify(_pool_summary())


@app.route("/api/pool/cache/stats")
def api_pool_cache_stats():
    stats = []
    for ag in _pool.get_agents(healthy_only=False):
        r = ag.cache_stats()
        if r:
            r["node_id"] = ag.node_id
            stats.append(r)
    return jsonify({
        "agents":     stats,
        "total_keys": sum(s.get("keys",    0) for s in stats),
        "total_mb":   sum(s.get("used_mb", 0) for s in stats),
    })


@app.route("/api/pool/cache/flush", methods=["POST"])
def api_pool_cache_flush():
    prefix = (request.json or {}).get("prefix", "")
    total  = _pool.cache_flush_all(prefix)
    log(f"[Pool] 🗑️  {total} önbellek anahtarı temizlendi")
    return jsonify({"ok": True, "flushed": total})


@app.route("/api/pool/storage")
def api_pool_storage():
    result = []
    for ag in _pool.get_agents(healthy_only=False):
        r = ag.storage_stats()
        if r:
            r["node_id"] = ag.node_id
            result.append(r)
    return jsonify({"agents": result})


@app.route("/api/pool/task", methods=["POST"])
def api_pool_task():
    d = request.json or {}
    result = _pool.run_task(
        d.get("type", "echo"),
        d.get("payload", {}),
        wait=d.get("wait", True),
        timeout=d.get("timeout", 30),
    )
    return jsonify({"ok": result is not None, "result": result})


@app.route("/api/pool/proxy/start", methods=["POST"])
def api_pool_proxy_start():
    d    = request.json or {}
    host = d.get("host", "127.0.0.1")
    port = int(d.get("port", 25565))
    started = _pool.start_proxies(host, port)
    log(f"[Pool] 🔀 {len(started)} agent'ta proxy başlatıldı")
    return jsonify({"ok": True, "started": started})


@app.route("/api/pool/proxy/stop", methods=["POST"])
def api_pool_proxy_stop():
    _pool.stop_proxies()
    return jsonify({"ok": True})


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
    f=MC_DIR/"server.properties"
    if not f.exists(): return jsonify({})
    props={}
    for line in f.read_text().splitlines():
        line=line.strip()
        if line and not line.startswith("#") and "=" in line:
            k,v=line.split("=",1); props[k.strip()]=v.strip()
    return jsonify(props)

@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    f=MC_DIR/"server.properties"; existing={}
    if f.exists():
        for line in f.read_text().splitlines():
            line=line.strip()
            if line and not line.startswith("#") and "=" in line:
                k,v=line.split("=",1); existing[k.strip()]=v.strip()
    existing.update(request.json or {})
    f.write_text("\n".join(f"{k}={v}" for k,v in existing.items())+"\n")
    return jsonify({"ok":True,"msg":"Kaydedildi. Yeniden başlatın."})

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
    send_command("save-off"); time.sleep(1); send_command("save-all"); time.sleep(2)
    with zipfile.ZipFile(str(dest),"w",zipfile.ZIP_DEFLATED) as z:
        for fp in src.rglob("*"):
            if fp.is_file(): z.write(fp,fp.relative_to(MC_DIR))
    send_command("save-on")
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
        cpu  = psutil.cpu_percent(0.2)
        vm   = psutil.virtual_memory()
        swp  = psutil.swap_memory()
        dk   = psutil.disk_usage("/")
        mc_info = {}
        if mc_process and mc_process.poll() is None:
            try:
                proc = psutil.Process(mc_process.pid)
                mc_info = {
                    "cpu":     round(proc.cpu_percent(), 1),
                    "ram":     int(proc.memory_info().rss / 1024 / 1024),
                    "threads": proc.num_threads(),
                }
            except: pass

        pool_info = _pool_summary()
        res  = pool_info.get("resources", {})

        # ── Ana sunucu — Render sınırlı kaynak hesabı ────────────
        # psutil host makinesini okur (10-30GB RAM, 400GB+ disk) — yanlış.
        # Render free: 512MB RAM, 18GB disk tahsis eder.

        # RAM: kendi process RSS üzerinden hesapla
        try:
            import psutil as _ps2
            my_rss_mb = int(_ps2.Process(os.getpid()).memory_info().rss / 1024 / 1024)
        except:
            my_rss_mb = 200
        _ram_cap_mb       = RENDER_RAM_LIMIT_MB
        ram_used_capped_mb = max(my_rss_mb, 150)
        main_ram_free_mb   = max(0, _ram_cap_mb - ram_used_capped_mb)
        ram_pct_capped     = round(ram_used_capped_mb / _ram_cap_mb * 100, 1)

        # Disk: MC dizini + swap dosyaları gerçek kullanımı
        try:
            _mc_used = sum(
                f.stat().st_size for f in MC_DIR.rglob("*") if f.is_file()
            ) / 1e9 if MC_DIR.exists() else 0
        except:
            _mc_used = 0
        _swap_used = sum(
            os.path.getsize(f) for f in ["/swapfile", "/swapfile2", "/swapfile_mc"]
            if os.path.exists(f)
        ) / 1e9
        disk_used_capped_gb = round(min(_mc_used + _swap_used + 3.5, RENDER_DISK_LIMIT_GB), 1)
        main_disk_free_gb   = round(max(0.0, RENDER_DISK_LIMIT_GB - disk_used_capped_gb), 1)
        disk_pct_capped     = round(disk_used_capped_gb / RENDER_DISK_LIMIT_GB * 100, 1)

        agent_ram_mb      = res.get("ram_free_mb",  0)
        agent_disk_gb     = res.get("disk_free_gb", 0)
        combined_ram_mb   = main_ram_free_mb + agent_ram_mb
        combined_disk_gb  = round(main_disk_free_gb + agent_disk_gb, 1)

        # Agent başına detay — resource_pool.AgentClient kullanılır
        agents_detail = []
        for a in _pool.get_agents(healthy_only=False):
            r   = a.info.get("ram",  {})
            d_  = a.info.get("disk", {})
            c   = a.info.get("cpu",  {})
            agents_detail.append({
                "node_id":    a.node_id,
                "healthy":    a.healthy,
                "ram_free":   r.get("free_mb",  0),
                "ram_cache":  r.get("cache_mb", 0),
                "disk_free":  round(d_.get("free_gb",  0), 1),
                "disk_store": round(d_.get("store_gb", 0), 1),
                "cpu_load":   c.get("load1", 0),
                "cpu_cores":  c.get("cores", 0),
                "last_ping":  int(time.time() - a.last_ok),
            })

        return jsonify({
            # Ana sunucu
            "cpu":           round(cpu, 1),
            "ram_pct":       ram_pct_capped,
            "ram_used_mb":   ram_used_capped_mb,
            "ram_total_mb":  _ram_cap_mb,
            "ram_free_mb":   main_ram_free_mb,
            "swap_total_mb": int(swp.total  / 1024 / 1024),
            "swap_used_mb":  int(swp.used   / 1024 / 1024),
            "swap_free_mb":  int(swp.free   / 1024 / 1024),
            "swap_pct":      round(swp.percent, 1),
            "disk_pct":      disk_pct_capped,
            "disk_used_gb":  disk_used_capped_gb,
            "disk_total_gb": RENDER_DISK_LIMIT_GB,
            "disk_free_gb":  main_disk_free_gb,
            "cpu_count":     psutil.cpu_count(),
            "mc":            mc_info,
            # MC server
            "tps":           server_state["tps"],
            "tps5":          server_state["tps5"],
            "tps15":         server_state["tps15"],
            # Pool (agentlar)
            "pool_agents":   pool_info["healthy"],
            "pool_ram_mb":   agent_ram_mb,
            "pool_disk_gb":  agent_disk_gb,
            "pool_cache_mb": res.get("cache_used_mb", 0),
            "pool_cpu":      res.get("cpu_cores", 0),
            # Birleşik
            "combined_ram_free_mb":  combined_ram_mb,
            "combined_disk_free_gb": combined_disk_gb,
            # Agent detayları
            "agents": agents_detail,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── SocketIO ──────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("console_history", list(console_buf))
    emit("server_status",   server_state)
    emit("players_update",  _players_list())
    emit("tunnel_update",   tunnel_info)
    emit("pool_update",     _pool_summary())


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
    <div class="sb-ver" id="sb-ver">Paper MC • v10.0</div>
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
    <span id="pool-res-bar" style="color:var(--a1);font-family:var(--mono);font-size:10px;margin-left:auto"></span>
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
        <button class="btn b-ghost" onclick="cmd('save-all')">💾 Kaydet</button>
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
    <!-- Birleşik özet kartlar -->
    <div class="g4" style="margin-bottom:14px">
      <div class="sc">
        <div class="sc-val" id="p-comb-ram">—</div>
        <div class="sc-lbl">🧠 Toplam Boş RAM</div>
        <div style="font-size:9px;color:var(--t3);margin-top:3px">Ana + Agentlar</div>
      </div>
      <div class="sc">
        <div class="sc-val" id="p-comb-disk">—</div>
        <div class="sc-lbl">💾 Toplam Boş Disk</div>
        <div style="font-size:9px;color:var(--t3);margin-top:3px">Ana + Agentlar</div>
      </div>
      <div class="sc">
        <div class="sc-val" id="p-tps-big">20.0</div>
        <div class="sc-lbl">⚡ TPS</div>
        <div style="font-size:9px;color:var(--t3);margin-top:3px">1m / 5m / 15m</div>
      </div>
      <div class="sc">
        <div class="sc-val" id="p-mcram-big">—</div>
        <div class="sc-lbl">☕ MC JVM RAM</div>
        <div style="font-size:9px;color:var(--t3);margin-top:3px">Heap kullanımı</div>
      </div>
    </div>

    <div class="g2" style="margin-bottom:14px">
      <!-- Ana sunucu -->
      <div class="card">
        <div class="card-hd">💻 Ana Sunucu (Render Free)</div>
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px">
            <span>CPU</span><span id="p-cpu">—</span>
          </div>
          <div class="prog"><div class="prog-f pf-cpu" id="pb-cpu" style="width:0%"></div></div>
        </div>
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px">
            <span>RAM <span style="font-size:9px;color:var(--red)">(512MB limit!)</span></span>
            <span id="p-ram">—</span>
          </div>
          <div class="prog"><div class="prog-f pf-ram" id="pb-ram" style="width:0%"></div></div>
        </div>
        <div style="margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px">
            <span>Disk <span style="font-size:9px">(18GB limit)</span></span>
            <span id="p-disk">—</span>
          </div>
          <div class="prog"><div class="prog-f pf-disk" id="pb-disk" style="width:0%"></div></div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px">
            <span>Swap</span><span id="p-swap-bar-lbl">—</span>
          </div>
          <div class="prog"><div class="prog-f" id="pb-swap" style="width:0%;background:linear-gradient(90deg,var(--yellow),var(--a4))"></div></div>
        </div>
      </div>

      <!-- MC + Pool özet -->
      <div class="card">
        <div class="card-hd">⛏️ MC + Pool Özeti</div>
        <table class="tbl">
          <tr><td style="color:var(--t2)">MC JVM RAM</td><td id="p-mcram" style="font-family:var(--mono);color:var(--a1)">—</td></tr>
          <tr><td style="color:var(--t2)">TPS (1m/5m/15m)</td><td id="p-tps1" style="font-family:var(--mono)">—</td></tr>
          <tr><td style="color:var(--t2)">Swap (Ana)</td><td id="p-swap" style="font-family:var(--mono)">—</td></tr>
          <tr><td style="color:var(--t2)">Agent Sayısı</td><td id="p-agents" style="font-family:var(--mono);color:var(--a2)">—</td></tr>
          <tr><td style="color:var(--t2)">Pool RAM Cache</td><td id="p-pcache" style="font-family:var(--mono);color:var(--a3)">—</td></tr>
          <tr><td style="color:var(--t2)">Pool Disk Deposu</td><td id="p-pdisk" style="font-family:var(--mono);color:var(--a3)">—</td></tr>
          <tr><td style="color:var(--t2)">Pool CPU Core</td><td id="p-pcpu" style="font-family:var(--mono)">—</td></tr>
        </table>
      </div>
    </div>

    <!-- Destek Düğümleri detay tablosu -->
    <div class="card" id="perf-agents-card">
      <div class="card-hd" style="justify-content:space-between">
        <span>🔗 Destek Düğümleri — Birleşik Kapasite</span>
        <button class="btn btn-sm b-ghost" onclick="loadPerf()">↺ Yenile</button>
      </div>
      <div id="perf-agents-table">
        <div style="text-align:center;padding:20px;color:var(--t2);font-size:12px">
          Bağlı agent yok — diğer Render hesabına agent.Dockerfile yükleyin
        </div>
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
  const v=document.getElementById('sb-ver');if(v&&d.version&&d.version!=='—')v.textContent='Paper MC • '+d.version;
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

async function loadPerf(){
  const d=await fetch('/api/performance').then(r=>r.json()).catch(()=>({}));
  if(d.cpu!==undefined){
    document.getElementById('p-cpu').textContent=d.cpu+'%';
    document.getElementById('pb-cpu').style.width=d.cpu+'%';
  }
  if(d.ram_pct!==undefined){
    const pct=d.ram_pct;
    document.getElementById('p-ram').textContent=`${d.ram_used_mb}/${d.ram_total_mb}MB`;
    document.getElementById('pb-ram').style.width=pct+'%';
    document.getElementById('pb-ram').style.background=pct>85?'linear-gradient(90deg,#ff4757,#ff6b35)':pct>70?'linear-gradient(90deg,#ffa502,var(--a1))':'linear-gradient(90deg,var(--a3),var(--a1))';
  }
  if(d.disk_pct!==undefined){
    document.getElementById('p-disk').textContent=`${d.disk_used_gb}/${d.disk_total_gb}GB`;
    document.getElementById('pb-disk').style.width=d.disk_pct+'%';
  }
  if(d.swap_pct!==undefined){
    document.getElementById('p-swap-bar-lbl').textContent=`${d.swap_used_mb||0}/${d.swap_total_mb||0}MB (%${d.swap_pct||0})`;
    document.getElementById('pb-swap').style.width=(d.swap_pct||0)+'%';
    document.getElementById('p-swap').textContent=`${d.swap_total_mb||0}MB / ${d.swap_free_mb||0}MB boş`;
  }
  // MC
  if(d.mc&&d.mc.ram){
    document.getElementById('p-mcram').textContent=d.mc.ram+' MB';
    document.getElementById('p-mcram-big').textContent=d.mc.ram+'MB';
  }
  // TPS
  const tpsVal=d.tps||'—';
  document.getElementById('p-tps1').textContent=`${d.tps||'—'} / ${d.tps5||'—'} / ${d.tps15||'—'}`;
  document.getElementById('p-tps-big').textContent=tpsVal;
  // Pool
  document.getElementById('p-agents').textContent=`${d.pool_agents||0} aktif`;
  document.getElementById('p-pcache').textContent=`${d.pool_cache_mb||0} MB`;
  document.getElementById('p-pdisk').textContent=`${d.pool_disk_gb||0} GB`;
  document.getElementById('p-pcpu').textContent=`${d.pool_cpu||0} core`;
  // Birleşik
  document.getElementById('p-comb-ram').textContent=(d.combined_ram_free_mb||0)+'MB';
  document.getElementById('p-comb-disk').textContent=(d.combined_disk_free_gb||0)+'GB';
  // Agent tablosu
  const agents=d.agents||[];
  const tbl=document.getElementById('perf-agents-table');
  if(!agents.length){
    tbl.innerHTML='<div style="text-align:center;padding:20px;color:var(--t2);font-size:12px">Bağlı agent yok — diğer Render hesabına agent.Dockerfile yükleyin</div>';
  } else {
    // Toplam satırı
    const totRamFree=agents.reduce((s,a)=>s+(a.ram_free||0),0);
    const totRamCache=agents.reduce((s,a)=>s+(a.ram_cache||0),0);
    const totDiskFree=agents.reduce((s,a)=>s+(a.disk_free||0),0).toFixed(1);
    const totDiskStore=agents.reduce((s,a)=>s+(a.disk_store||0),0).toFixed(1);
    const totCores=agents.reduce((s,a)=>s+(a.cpu_cores||0),0);
    tbl.innerHTML=`
    <table class="tbl">
      <thead>
        <tr>
          <th>Düğüm</th><th>Durum</th>
          <th>RAM Boş</th><th>RAM Cache</th>
          <th>Disk Boş</th><th>Disk Depo</th>
          <th>CPU</th><th>Son Ping</th>
        </tr>
      </thead>
      <tbody>
        ${agents.map(a=>`
        <tr>
          <td><strong style="font-family:var(--mono);font-size:11px">${a.node_id}</strong></td>
          <td><span class="badge ${a.healthy?'bg':'br'}">${a.healthy?'✅ Sağlıklı':'❌ Erişilemiyor'}</span></td>
          <td style="font-family:var(--mono);color:var(--a1)">${a.ram_free}MB</td>
          <td style="font-family:var(--mono);color:var(--a3)">${a.ram_cache}MB</td>
          <td style="font-family:var(--mono);color:var(--a2)">${a.disk_free}GB</td>
          <td style="font-family:var(--mono)">${a.disk_store}GB</td>
          <td style="font-family:var(--mono)">${a.cpu_cores}c / ${a.cpu_load}</td>
          <td style="font-size:10px;color:var(--t3)">${a.last_ping}s</td>
        </tr>`).join('')}
        <tr style="background:rgba(124,106,255,.06);font-weight:700">
          <td colspan="2" style="color:var(--a2)">📊 TOPLAM (${agents.length} agent)</td>
          <td style="font-family:var(--mono);color:var(--a1)">${totRamFree}MB</td>
          <td style="font-family:var(--mono);color:var(--a3)">${totRamCache}MB</td>
          <td style="font-family:var(--mono);color:var(--a2)">${totDiskFree}GB</td>
          <td style="font-family:var(--mono)">${totDiskStore}GB</td>
          <td style="font-family:var(--mono)">${totCores} core</td>
          <td></td>
        </tr>
        <tr style="background:rgba(0,229,255,.04)">
          <td colspan="2" style="color:var(--a1);font-weight:700">🌐 BİRLEŞİK (Ana+Agentlar)</td>
          <td colspan="2" style="font-family:var(--mono);color:var(--a1);font-weight:700">${d.combined_ram_free_mb}MB boş RAM</td>
          <td colspan="2" style="font-family:var(--mono);color:var(--a2);font-weight:700">${d.combined_disk_free_gb}GB boş Disk</td>
          <td colspan="2"></td>
        </tr>
      </tbody>
    </table>`;
  }
}
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
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════

threading.Thread(target=_ram_monitor,        daemon=True).start()
threading.Thread(target=_ram_watchdog,       daemon=True).start()
# _pool_health_watchdog KALDIRILDI: resource_pool.health_monitor() zaten arka planda çalışıyor
threading.Thread(target=_pool_auto_optimize,  daemon=True).start()
threading.Thread(target=_world_backup_loop,   daemon=True).start()  # Agent disk doldur
threading.Thread(target=_ram_cache_warm_loop, daemon=True).start()  # Agent RAM doldur
_pool.set_logger(log)   # Panel log fonksiyonunu pool'a inject et

if __name__ == "__main__":
    MC_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[MC Panel v10.0] :{PANEL_PORT} başlatılıyor...")
    socketio.run(app, host="0.0.0.0", port=PANEL_PORT,
                 debug=False, use_reloader=False, log_output=False)
