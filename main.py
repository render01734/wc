"""
⛏️  Minecraft Server Boot — RENDER BYPASS v9.2
FIX v9.2:
  - _support_register: sonsuz retry (her 30sn), vazgeçmez
  - _support_heartbeat: host bilgisini de gönderir
  - try_connect_worker: sonsuz döngü, timeout yok
  - api_worker_heartbeat: host varsa NBD bağlantısını tetikler
  - IS_MAIN: RENDER_EXTERNAL_URL env'e güvenir
"""

import os, sys, subprocess, time, socket, resource, threading, re, glob
import psutil

RENDER_RAM_LIMIT_MB  = 512
RENDER_DISK_LIMIT_GB = 18.0

def read_cgroup_ram_limit_mb():
    for path in [
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ]:
        try:
            val = open(path).read().strip()
            if val in ("max", "-1"):
                continue
            limit_mb = int(val) // 1024 // 1024
            if 64 < limit_mb < 65536:
                return limit_mb
        except: pass
    return RENDER_RAM_LIMIT_MB

def read_actual_disk_used_gb():
    try:
        total = 0
        for f in ["/nbd_disk.img", "/swapfile", "/tmp/nbd_ram.img"]:
            if os.path.exists(f):
                total += os.path.getsize(f)
        return 4.0 + total / 1024**3
    except:
        return 4.0

CONTAINER_RAM_MB = read_cgroup_ram_limit_mb()

MAIN_SERVER_URL = "https://wc-tsgd.onrender.com"
MY_URL  = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
IS_MAIN = (MY_URL == MAIN_SERVER_URL or MY_URL == ""
           or os.environ.get("FORCE_MAIN", "") == "1")

PORT    = int(os.environ.get("PORT", "5000"))
MC_PORT = 25565
MC_RAM  = os.environ.get("MC_RAM", "2G")

print("\n" + "━"*56)
print("  ⛏️   Minecraft Server — RENDER BYPASS v9.2")
print(f"      MOD      : {'🟢 ANA SUNUCU' if IS_MAIN else '🔵 DESTEK SUNUCUSU'}")
print(f"      MY_URL   : {MY_URL or '(boş — ANA kabul)'}")
print(f"      RAM LİMİT: {CONTAINER_RAM_MB}MB")
print("━"*56 + "\n")

base_env = {
    **os.environ,
    "HOME": "/root", "USER": "root", "LOGNAME": "root",
    "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
    "JAVA_HOME": "/usr/lib/jvm/java-21-openjdk-amd64",
    "PATH": "/usr/lib/jvm/java-21-openjdk-amd64/bin"
            ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "MC_RAM": MC_RAM, "PORT": str(PORT),
    "CONTAINER_RAM_MB": str(CONTAINER_RAM_MB),
}
INF = resource.RLIM_INFINITY

def w(path, val):
    try:
        with open(path, "w") as f: f.write(str(val))
        return True
    except: return False

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True)

def wait_port(port, timeout=60):
    for _ in range(timeout * 10):
        try:
            s = socket.create_connection(("127.0.0.1", int(port)), 0.1)
            s.close(); return True
        except OSError:
            time.sleep(0.1)
    return False

def calc_swap_budget_mb():
    used_gb   = read_actual_disk_used_gb()
    available = RENDER_DISK_LIMIT_GB - used_gb - 7.0
    return int(min(3.0, max(0.0, available)) * 1024)

def calc_nbd_disk_gb():
    used_gb   = read_actual_disk_used_gb()
    available = RENDER_DISK_LIMIT_GB - used_gb - 3.0
    return min(11.0, max(0.5, available))

def calc_ram_disk_mb():
    available = CONTAINER_RAM_MB - 150 - 62
    return min(200, max(0, available))

# ── cgroup bypass ─────────────────────────────────────────────
def bypass_cgroups():
    print("  [cgroup] Limitler kaldırılıyor...")
    n = 0
    for path, val in [
        ("/sys/fs/cgroup/memory.max",      "max"),
        ("/sys/fs/cgroup/memory.swap.max", "max"),
        ("/sys/fs/cgroup/memory.high",     "max"),
        ("/sys/fs/cgroup/cpu.max",         "max"),
        ("/sys/fs/cgroup/pids.max",        "max"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes",       "-1"),
        ("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes", "-1"),
        ("/sys/fs/cgroup/memory/memory.soft_limit_in_bytes",  "-1"),
        ("/sys/fs/cgroup/memory/memory.swappiness",           "100"),
        ("/sys/fs/cgroup/memory/memory.oom_control",          "0"),
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us",               "-1"),
    ]:
        if w(path, val): n += 1
    for cg in glob.glob("/sys/fs/cgroup/*/") + glob.glob("/sys/fs/cgroup/*/*/"):
        for fn, v in [("memory.max","max"),("memory.swap.max","max"),
                      ("memory.high","max"),("memory.oom_control","0"),
                      ("cpu.max","max"),("pids.max","max")]:
            w(cg + fn, v)
    w("/proc/sys/vm/oom_kill_allocating_task", "0")
    w("/proc/sys/vm/panic_on_oom", "0")
    try: w(f"/proc/{os.getpid()}/oom_score_adj", "-1000")
    except: pass
    print(f"  ✅ {n} cgroup limiti kaldırıldı")

def setup_swap(mode="main"):
    sh("modprobe zram num_devices=1 2>/dev/null")
    w("/sys/block/zram0/comp_algorithm", "lz4")
    if w("/sys/block/zram0/disksize", "128M"):
        if sh("mkswap /dev/zram0 && swapon -p 100 /dev/zram0").returncode == 0:
            print("  ✅ zram: 128MB")

    if mode == "main":
        swap_mb = calc_swap_budget_mb()
        if swap_mb >= 256:
            swap_file = "/swapfile"
            if os.path.exists(swap_file):
                sh(f"swapoff {swap_file} 2>/dev/null")
                try: os.remove(swap_file)
                except: pass
            r = sh(f"fallocate -l {swap_mb}M {swap_file}")
            if r.returncode != 0:
                sh(f"dd if=/dev/zero of={swap_file} bs=64M count={max(1,swap_mb//64)} status=none")
            sh(f"chmod 600 {swap_file} && mkswap -f {swap_file}")
            if sh(f"swapon -p 0 {swap_file}").returncode == 0:
                print(f"  ✅ Swap dosyası: {swap_mb}MB")

    for path, val in [
        ("/proc/sys/vm/swappiness",             "100"),
        ("/proc/sys/vm/vfs_cache_pressure",     "200"),
        ("/proc/sys/vm/overcommit_memory",      "1"),
        ("/proc/sys/vm/overcommit_ratio",       "100"),
        ("/proc/sys/vm/page-cluster",           "0"),
        ("/proc/sys/vm/drop_caches",            "3"),
        ("/proc/sys/vm/watermark_boost_factor", "0"),
        ("/proc/sys/vm/min_free_kbytes",        "32768"),
    ]:
        w(path, val)

def optimize_kernel():
    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (INF, INF)),
        (resource.RLIMIT_MEMLOCK, (INF, INF)),
    ]:
        try: resource.setrlimit(res, val)
        except: pass

def optimize_all(mode="main"):
    print(f"\n{'═'*56}")
    print(f"  🔓 BYPASS ({mode.upper()})")
    print(f"{'═'*56}\n")
    bypass_cgroups()
    setup_swap(mode)
    optimize_kernel()
    swp = psutil.swap_memory()
    print(f"  ✅ Swap: {swp.total//1024//1024}MB | RAM limit: {CONTAINER_RAM_MB}MB")
    print(f"{'═'*56}")


# ══════════════════════════════════════════════════════════════
#  ANA SUNUCU
# ══════════════════════════════════════════════════════════════

# NBD bağlantısı yapıldı mı?
_nbd_connected = False
_nbd_lock      = threading.Lock()

def start_panel():
    print(f"\n🚀 Panel başlatılıyor (:{PORT})...")
    proc = subprocess.Popen([sys.executable, "/app/mc_panel.py"], env=base_env)
    if wait_port(PORT, 30):
        print(f"  ✅ Panel hazır")
    return proc


def auto_start_sequence():
    time.sleep(2)
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://localhost:{PORT}/api/start",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        print("  ✅ MC başlatma komutu gönderildi")
    except Exception as e:
        print(f"  ⚠️  MC start: {e}")

    if wait_port(MC_PORT, 300):
        print("  ✅ MC Server hazır!")
    else:
        print("  ⚠️  MC portu timeout")

    log_file = "/tmp/cf_mc.log"
    subprocess.Popen([
        "cloudflared", "tunnel",
        "--url", f"tcp://localhost:{MC_PORT}",
        "--no-autoupdate", "--loglevel", "info",
    ], stdout=open(log_file, "w"), stderr=subprocess.STDOUT)

    for _ in range(120):
        try:
            content = open(log_file).read()
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', content)
            if urls:
                import json as _j, urllib.request as _ur
                tunnel_url = urls[0]
                host = tunnel_url.replace("https://", "")
                print(f"\n  ✅ MC Adres: {host}\n")
                try:
                    data = _j.dumps({"url": tunnel_url, "host": host}).encode()
                    req2 = _ur.Request(
                        f"http://localhost:{PORT}/api/internal/tunnel",
                        data=data, headers={"Content-Type": "application/json"}, method="POST"
                    )
                    _ur.urlopen(req2, timeout=3)
                except: pass
                return
        except: pass
        time.sleep(0.5)


def connect_worker_nbd(host: str, tunnel_port: int = 10810):
    """NBD over cloudflared TCP tunnel. Bir kez bağlanır."""
    global _nbd_connected
    with _nbd_lock:
        if _nbd_connected:
            print(f"  [nbd] Zaten bağlı, atlanıyor")
            return True

    print(f"\n  [nbd] Bağlanılıyor: {host}...")
    sh("modprobe nbd max_part=0 2>/dev/null")

    subprocess.Popen([
        "cloudflared", "access", "tcp",
        "--hostname", host,
        "--url", f"localhost:{tunnel_port}",
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(4)

    connected = False
    for dev, export, prio in [("/dev/nbd0", "ram", 10), ("/dev/nbd1", "disk", 5)]:
        ret = sh(f"nbd-client localhost {tunnel_port} {dev} -N {export} -b 4096 -t 60 2>&1")
        if ret.returncode == 0:
            sh(f"mkswap {dev}")
            if sh(f"swapon -p {prio} {dev}").returncode == 0:
                print(f"  ✅ {dev} ({export}) swap'a eklendi (öncelik:{prio})")
                connected = True

    if connected:
        with _nbd_lock:
            _nbd_connected = True
        swp = psutil.swap_memory()
        print(f"  🎯 YENİ SWAP: {swp.total//1024//1024}MB")
        # Paneli bilgilendir
        try:
            import urllib.request as _ur, json as _j
            data = _j.dumps({"nbd_connected": True, "host": host}).encode()
            _ur.Request(f"http://localhost:{PORT}/api/internal/nbd_status",
                        data=data, headers={"Content-Type": "application/json"}, method="POST")
        except: pass
    return connected


def try_connect_worker():
    """
    FIX v9.2: Sonsuz döngü — hiç vazgeçme.
    Panel API'yi poll et (subprocess cross-process sorunu aşılır).
    Heartbeat'ten gelen host bilgisini de kontrol et.
    """
    import urllib.request as _ur, json as _j

    # Panel hazır olana kadar bekle
    print("  [worker] Panel bekleniyor...")
    for _ in range(60):
        try:
            _ur.urlopen(f"http://localhost:{PORT}/", timeout=2)
            break
        except: time.sleep(1)

    # Env'den direkt host
    host = os.environ.get("WORKER_HOST", "").strip()
    if host:
        print(f"  [worker] WORKER_HOST env: {host}")
        connect_worker_nbd(host)
        return

    print("  [worker] Destek sunucusu bekleniyor (sonsuz döngü)...")
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = _ur.urlopen(
                f"http://localhost:{PORT}/api/worker/status", timeout=5
            )
            data = _j.loads(resp.read())
            nodes = data.get("nodes", [])
            if nodes:
                host = nodes[0].get("host", "")
                if host:
                    print(f"  [worker] ✅ Destek bulundu (deneme {attempt}): {host}")
                    connect_worker_nbd(host)
                    return
        except Exception as e:
            pass

        if attempt % 30 == 0:
            print(f"  [worker] Hâlâ bekleniyor... ({attempt*5}sn geçti)")
        time.sleep(5)


# ══════════════════════════════════════════════════════════════
#  DESTEK SUNUCUSU
# ══════════════════════════════════════════════════════════════

SUPPORT_NBD_PORT = 10809
SUPPORT_NBD_FILE = "/nbd_disk.img"
SUPPORT_RAM_FILE = "/tmp/nbd_ram.img"
SUPPORT_NODE_ID  = (MY_URL.replace("https://","").replace(".onrender.com","") or "support")


def support_install_tools():
    import shutil as _s
    missing = [t for t in ["nbd-server","socat","nbd-client"] if not _s.which(t)]
    if missing:
        sh(f"apt-get update -qq 2>/dev/null && DEBIAN_FRONTEND=noninteractive "
           f"apt-get install -y --no-install-recommends {' '.join(missing)}")
        print(f"  [destek] ✅ Kuruldu: {', '.join(missing)}")


def support_create_ram_disk():
    ram_disk_mb = calc_ram_disk_mb()
    if ram_disk_mb <= 0:
        return 0
    if os.path.exists(SUPPORT_RAM_FILE):
        existing = os.path.getsize(SUPPORT_RAM_FILE) // 1024 // 1024
        if existing >= ram_disk_mb * 0.85:
            print(f"  [destek] ✅ RAM disk: {existing}MB"); return existing
        try: os.remove(SUPPORT_RAM_FILE)
        except: pass
    r = sh(f"fallocate -l {ram_disk_mb}M {SUPPORT_RAM_FILE}")
    if r.returncode != 0:
        sh(f"dd if=/dev/zero of={SUPPORT_RAM_FILE} bs=1M count={ram_disk_mb} status=none")
    if os.path.exists(SUPPORT_RAM_FILE):
        actual = os.path.getsize(SUPPORT_RAM_FILE) // 1024 // 1024
        print(f"  [destek] ✅ RAM disk: {actual}MB")
        return actual
    return 0


def support_create_disk_file():
    nbd_gb = calc_nbd_disk_gb()
    nbd_mb = int(nbd_gb * 1024)
    if nbd_gb < 0.5:
        return 0.0
    if os.path.exists(SUPPORT_NBD_FILE):
        existing = os.path.getsize(SUPPORT_NBD_FILE) / 1024**3
        if existing >= nbd_gb * 0.85:
            print(f"  [destek] ✅ NBD disk: {existing:.1f}GB"); return existing
        try: os.remove(SUPPORT_NBD_FILE)
        except: pass
    r = sh(f"fallocate -l {nbd_mb}M {SUPPORT_NBD_FILE}")
    if r.returncode != 0:
        for i in range(nbd_mb // 512):
            if read_actual_disk_used_gb() > RENDER_DISK_LIMIT_GB - 3.0:
                break
            sh(f"dd if=/dev/zero of={SUPPORT_NBD_FILE} bs=512M count=1 seek={i} conv=notrunc 2>/dev/null")
    actual = 0.0
    if os.path.exists(SUPPORT_NBD_FILE):
        actual = os.path.getsize(SUPPORT_NBD_FILE) / 1024**3
        print(f"  [destek] ✅ NBD disk: {actual:.1f}GB")
    return actual


def support_start_nbd(ram_disk_mb, disk_gb):
    support_install_tools()
    sh("modprobe nbd max_part=0 2>/dev/null")
    import shutil as _s
    if _s.which("nbd-server"):
        try:
            os.makedirs("/etc/nbd-server", exist_ok=True)
            cfg = f"[generic]\n    port = {SUPPORT_NBD_PORT}\n    allowlist = true\n"
            if ram_disk_mb > 0 and os.path.exists(SUPPORT_RAM_FILE):
                cfg += f"\n[ram]\n    exportname = {SUPPORT_RAM_FILE}\n    readonly = false\n"
            if disk_gb > 0 and os.path.exists(SUPPORT_NBD_FILE):
                cfg += f"\n[disk]\n    exportname = {SUPPORT_NBD_FILE}\n    readonly = false\n"
            open("/etc/nbd-server/config", "w").write(cfg)
            proc = subprocess.Popen(
                ["nbd-server", "-C", "/etc/nbd-server/config"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            time.sleep(2)
            if proc.poll() is None:
                print(f"  [destek] ✅ nbd-server aktif (:{SUPPORT_NBD_PORT})")
                return True
        except Exception as e:
            print(f"  [destek] ⚠️  nbd-server: {e}")
    if _s.which("socat"):
        target = SUPPORT_NBD_FILE if disk_gb > 0 else SUPPORT_RAM_FILE
        subprocess.Popen([
            "socat", f"TCP-LISTEN:{SUPPORT_NBD_PORT},reuseaddr,fork",
            f"FILE:{target},rdwr"
        ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(1)
        print(f"  [destek] ✅ socat fallback (:{SUPPORT_NBD_PORT})")
        return True
    return False


# ── FIX v9.2: Sonsuz retry + heartbeat host bilgisi ──────────

def _support_register_loop(url, host, ram_disk_mb, disk_gb):
    """Ana sunucu uyanana kadar her 30sn'de bir retry — asla vazgeçme."""
    import urllib.request as _ur, json as _j

    payload = _j.dumps({
        "worker_host":   host,
        "worker_url":    url,
        "nbd_gb":        round(disk_gb, 1),
        "ram_disk_mb":   ram_disk_mb,
        "node_id":       SUPPORT_NODE_ID,
        "ram_limit_mb":  CONTAINER_RAM_MB,
        "disk_limit_gb": RENDER_DISK_LIMIT_GB,
    }).encode()

    attempt = 0
    while True:
        attempt += 1
        try:
            req = _ur.Request(
                f"{MAIN_SERVER_URL}/api/worker/register",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            _ur.urlopen(req, timeout=20)
            print(f"  [destek] ✅ Kayıt başarılı (deneme {attempt}): {MAIN_SERVER_URL}")
            return   # başarılı → döngüden çık, heartbeat devralır
        except Exception as e:
            print(f"  [destek] ⚠️  Kayıt başarısız (deneme {attempt}): {e}")
            print(f"  [destek]    Ana sunucu uyanıyor olabilir. 30sn sonra tekrar...")
            time.sleep(30)


def _support_heartbeat(url, host, ram_disk_mb, disk_gb):
    """
    FIX v9.2: Heartbeat artık tam worker bilgisini içeriyor.
    Ana sunucu restart olsa bile yeni heartbeat üzerinden yeniden register eder.
    """
    import urllib.request as _ur, json as _j

    while True:
        time.sleep(25)
        try:
            try:
                vmrss = int([l for l in open("/proc/self/status")
                             if l.startswith("VmRSS:")][0].split()[1])
                rss_mb = vmrss // 1024
            except: rss_mb = 0

            # Hem heartbeat hem de tam kayıt bilgisi gönder
            data = _j.dumps({
                "node_id":       SUPPORT_NODE_ID,
                "worker_host":   host,       # ← FIX: host her zaman gönder
                "worker_url":    url,
                "nbd_gb":        round(disk_gb, 1),
                "ram_disk_mb":   ram_disk_mb,
                "rss_mb":        rss_mb,
                "disk_used_gb":  round(read_actual_disk_used_gb(), 1),
                "ram_limit_mb":  CONTAINER_RAM_MB,
                "disk_limit_gb": RENDER_DISK_LIMIT_GB,
            }).encode()

            req = _ur.Request(
                f"{MAIN_SERVER_URL}/api/worker/heartbeat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            _ur.urlopen(req, timeout=10)
        except: pass


def _support_ram_watchdog():
    limit_mb = CONTAINER_RAM_MB
    while True:
        time.sleep(8)
        try:
            vmrss = int([l for l in open("/proc/self/status")
                         if l.startswith("VmRSS:")][0].split()[1])
            rss_mb = vmrss // 1024
            pct = rss_mb / limit_mb * 100
            if pct > 95:
                w("/proc/sys/vm/drop_caches", "3")
                if os.path.exists(SUPPORT_RAM_FILE):
                    try: os.remove(SUPPORT_RAM_FILE)
                    except: pass
            elif pct > 90:
                w("/proc/sys/vm/drop_caches", "1")
        except: pass


def support_start_tunnel(ram_disk_mb, disk_gb):
    log = "/tmp/cf_support.log"
    print(f"  [destek] 🌐 Cloudflare tüneli başlatılıyor...")
    subprocess.Popen([
        "cloudflared", "tunnel",
        "--url", f"tcp://localhost:{SUPPORT_NBD_PORT}",
        "--no-autoupdate", "--loglevel", "info",
    ], stdout=open(log, "w"), stderr=subprocess.STDOUT)

    # URL için max 120sn bekle
    for i in range(240):
        try:
            content = open(log).read()
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', content)
            if urls:
                url  = urls[0]
                host = url.replace("https://", "")
                print(f"\n  [destek] ✅ Tünel hazır: {host}\n")

                # Kayıt döngüsünü thread'de başlat (sonsuz retry)
                threading.Thread(
                    target=_support_register_loop,
                    args=(url, host, ram_disk_mb, disk_gb),
                    daemon=True
                ).start()

                # Kayıt başarılı olduktan sonra heartbeat başlat
                threading.Thread(
                    target=_support_heartbeat,
                    args=(url, host, ram_disk_mb, disk_gb),
                    daemon=True
                ).start()
                return url
        except: pass
        time.sleep(0.5)

    print("  [destek] ⚠️  Tünel URL alınamadı (120sn timeout)")
    return ""


def run_support_mode():
    from flask import Flask, jsonify

    print("\n" + "═"*56)
    print("  🔵 DESTEK MODU v9.2")
    print(f"  RAM={CONTAINER_RAM_MB}MB  DISK={RENDER_DISK_LIMIT_GB}GB")
    print(f"  Ana sunucu: {MAIN_SERVER_URL}")
    print(f"  Node ID: {SUPPORT_NODE_ID}")
    print("═"*56 + "\n")

    ram_disk_mb = support_create_ram_disk()
    disk_gb     = support_create_disk_file()
    support_start_nbd(ram_disk_mb, disk_gb)

    threading.Thread(
        target=support_start_tunnel,
        args=(ram_disk_mb, disk_gb), daemon=True
    ).start()
    threading.Thread(target=_support_ram_watchdog, daemon=True).start()

    support_app = Flask(__name__)

    @support_app.route("/")
    @support_app.route("/health")
    def health():
        try:
            vmrss = int([l for l in open("/proc/self/status")
                         if l.startswith("VmRSS:")][0].split()[1])
            rss_mb = vmrss // 1024
        except: rss_mb = 0
        used_disk = read_actual_disk_used_gb()
        ram_pct   = min(100, int(rss_mb / CONTAINER_RAM_MB * 100))
        disk_pct  = min(100, int(used_disk / RENDER_DISK_LIMIT_GB * 100))
        rc = "#ff4757" if ram_pct > 85 else "#00e5ff"
        dc = "#ff4757" if disk_pct > 85 else "#00e5ff"
        swp = psutil.swap_memory()
        return SUPPORT_HTML.format(
            main_url=MAIN_SERVER_URL, node_id=SUPPORT_NODE_ID,
            ram_disk_mb=ram_disk_mb, disk_gb=f"{disk_gb:.1f}",
            rss_mb=rss_mb, ram_limit=CONTAINER_RAM_MB,
            ram_pct=ram_pct, ram_color=rc,
            disk_used=round(used_disk,1), disk_limit=RENDER_DISK_LIMIT_GB,
            disk_pct=disk_pct, disk_color=dc,
            swap_mb=swp.total//1024//1024,
        )

    @support_app.route("/api/worker/status")
    def status():
        try:
            vmrss = int([l for l in open("/proc/self/status")
                         if l.startswith("VmRSS:")][0].split()[1])
            rss_mb = vmrss // 1024
        except: rss_mb = 0
        return jsonify({
            "mode": "support", "node_id": SUPPORT_NODE_ID,
            "ram_disk_mb": ram_disk_mb, "disk_gb": round(disk_gb,1),
            "rss_mb": rss_mb, "ram_limit_mb": CONTAINER_RAM_MB,
            "disk_used_gb": round(read_actual_disk_used_gb(),1),
            "disk_limit_gb": RENDER_DISK_LIMIT_GB,
        })

    print(f"[Destek] Flask :{PORT}...")
    support_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


SUPPORT_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="8">
<title>🔵 Destek Sunucusu</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0b12;color:#eef0f8;font-family:'Segoe UI',sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#0f1120;border:1px solid rgba(124,106,255,.3);border-radius:16px;
  padding:28px 32px;max-width:520px;width:92%;text-align:center}}
h1{{font-size:19px;font-weight:700;margin-bottom:4px;color:#7c6aff}}
.sub{{font-size:11px;color:#8892a4;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:14px}}
.s{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:12px 10px}}
.sv{{font-size:17px;font-weight:700;font-family:monospace}}
.sl{{font-size:10px;color:#8892a4;margin-top:2px}}
.bar-wrap{{background:rgba(255,255,255,.06);border-radius:4px;height:4px;margin-top:5px;overflow:hidden}}
.bar{{height:100%;border-radius:4px}}
.limit-row{{display:flex;justify-content:space-between;font-size:10px;color:#8892a4;margin-top:4px}}
.badge{{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;
  font-size:10px;font-weight:700;background:rgba(124,106,255,.12);
  border:1px solid rgba(124,106,255,.3);color:#7c6aff;margin-bottom:13px}}
.dot{{width:7px;height:7px;border-radius:50%;background:#7c6aff;
  box-shadow:0 0 5px #7c6aff;animation:blink 1.5s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.link{{display:inline-block;margin-top:10px;padding:9px 22px;
  background:linear-gradient(135deg,#7c6aff,#00e5ff);color:#000;
  border-radius:8px;font-weight:700;text-decoration:none;font-size:12px}}
</style>
</head>
<body>
<div class="card">
  <div style="font-size:40px;margin-bottom:8px">🔵</div>
  <div class="badge"><div class="dot"></div> DESTEK MODU AKTİF</div>
  <h1>Destek Sunucusu</h1>
  <div class="sub">Node: {node_id} · 8sn'de yenilenir</div>
  <div class="grid">
    <div class="s">
      <div class="sv" style="color:{ram_color}">{rss_mb}MB</div>
      <div class="sl">🧠 RAM Kullanımı</div>
      <div class="bar-wrap"><div class="bar" style="width:{ram_pct}%;background:{ram_color}"></div></div>
      <div class="limit-row"><span>%{ram_pct}</span><span>/{ram_limit}MB</span></div>
    </div>
    <div class="s">
      <div class="sv" style="color:{disk_color}">{disk_used}GB</div>
      <div class="sl">💾 Disk Kullanımı</div>
      <div class="bar-wrap"><div class="bar" style="width:{disk_pct}%;background:{disk_color}"></div></div>
      <div class="limit-row"><span>%{disk_pct}</span><span>/{disk_limit}GB</span></div>
    </div>
    <div class="s">
      <div class="sv" style="color:#00e5ff">{ram_disk_mb}MB</div>
      <div class="sl">💡 Paylaşılan RAM Diski</div>
    </div>
    <div class="s">
      <div class="sv" style="color:#00e5ff">{disk_gb}GB</div>
      <div class="sl">📦 Paylaşılan NBD Disk</div>
    </div>
  </div>
  <a class="link" href="{main_url}" target="_blank">→ Ana Sunucuya Git</a>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════
#  mc_panel.py için /api/worker/heartbeat — host bilgisini işler
#  Bu fonksiyon mc_panel.py'ye taşınmalı (aşağıya bakın)
# ══════════════════════════════════════════════════════════════

mode = "main" if IS_MAIN else "support"
optimize_all(mode)

if IS_MAIN:
    print(f"\n{'━'*56}")
    print(f"  🟢 ANA SUNUCU v9.2 — Panel: http://0.0.0.0:{PORT}")
    print(f"{'━'*56}\n")
    panel_proc = start_panel()
    threading.Thread(target=auto_start_sequence, daemon=True).start()
    threading.Thread(target=try_connect_worker,  daemon=True).start()
    panel_proc.wait()
else:
    run_support_mode()
