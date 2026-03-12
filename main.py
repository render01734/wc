"""
⛏️  Minecraft Server Boot — RENDER BYPASS v9.3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v9.3 Değişiklikler:
  - wstunnel: TCP over WebSocket/HTTPS (trycloudflare uyumlu)
  - cloudflared access tcp KALDIRILDI (trycloudflare sadece HTTP/HTTPS)
  - Min 3 destek düğümü NBD hazır olmadan MC başlamaz (BARIYER)
  - Her düğüm: ayrı wstunnel port + /dev/nbdX + swapon
  - Paralel bağlantı thread'leri (çoklu düğüm)
  Akış:
    Destek: nbd-server → wstunnel server(8080) → cloudflared HTTP tüneli
    Ana:    cloudflare URL → wstunnel client → nbd-client(10810+) → swapon
"""

import os, sys, subprocess, time, socket, resource, threading, re, glob, json
import psutil
import urllib.request as _ur

RENDER_RAM_LIMIT_MB  = 512
RENDER_DISK_LIMIT_GB = 18.0
MIN_SUPPORT_NODES    = 3       # MC başlamadan beklenen min NBD bağlantı sayısı
WSTUNNEL_PORT        = 8080    # Destek: wstunnel server dinleme portu
NBD_SERVER_PORT      = 10809   # Destek: nbd-server portu
NBD_CLIENT_BASE      = 10810   # Ana: local wstunnel portları (10810, 10811, 10812...)

MAIN_SERVER_URL = "https://wc-tsgd.onrender.com"
MY_URL  = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
IS_MAIN = (MY_URL == MAIN_SERVER_URL
           or MY_URL == ""
           or os.environ.get("FORCE_MAIN", "") == "1")
PORT    = int(os.environ.get("PORT", "5000"))
MC_PORT = 25565
MC_RAM  = os.environ.get("MC_RAM", "2G")

SUPPORT_NODE_ID  = (MY_URL.replace("https://", "").replace(".onrender.com", "") or "support")
SUPPORT_NBD_FILE = "/nbd_disk.img"
INF = resource.RLIM_INFINITY


# ── Helpers ───────────────────────────────────────────────────

def read_cgroup_ram_limit_mb():
    for path in ["/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
        try:
            val = open(path).read().strip()
            if val in ("max", "-1"):
                continue
            mb = int(val) // 1024 // 1024
            if 64 < mb < 65536:
                return mb
        except:
            pass
    return RENDER_RAM_LIMIT_MB


def read_actual_disk_used_gb():
    try:
        total = sum(
            os.path.getsize(f)
            for f in ["/nbd_disk.img", "/swapfile", "/tmp/nbd_ram.img"]
            if os.path.exists(f)
        )
        return 4.0 + total / 1024**3
    except:
        return 4.0


CONTAINER_RAM_MB = read_cgroup_ram_limit_mb()

base_env = {
    **os.environ,
    "HOME": "/root", "USER": "root", "LOGNAME": "root",
    "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
    "JAVA_HOME": "/usr/lib/jvm/java-21-openjdk-amd64",
    "PATH": (
        "/usr/lib/jvm/java-21-openjdk-amd64/bin"
        ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ),
    "MC_RAM": MC_RAM,
    "PORT": str(PORT),
    "CONTAINER_RAM_MB": str(CONTAINER_RAM_MB),
    "MIN_SUPPORT_NODES": str(MIN_SUPPORT_NODES),
}

print("\n" + "━"*56)
print("  ⛏️   Minecraft Server — RENDER BYPASS v9.3")
print(f"      MOD       : {'🟢 ANA' if IS_MAIN else '🔵 DESTEK'}")
print(f"      MY_URL    : {MY_URL or '(boş → ANA)'}")
print(f"      RAM       : {CONTAINER_RAM_MB}MB")
if IS_MAIN:
    print(f"      MIN DESTEK: {MIN_SUPPORT_NODES} düğüm (wstunnel NBD)")
print("━"*56 + "\n")


def w(path, val):
    try:
        open(path, "w").write(str(val))
        return True
    except:
        return False


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True)


def wait_port(port, timeout=60):
    for _ in range(timeout * 10):
        try:
            s = socket.create_connection(("127.0.0.1", int(port)), 0.1)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def calc_swap_mb():
    used = read_actual_disk_used_gb()
    return int(min(3.0, max(0.0, RENDER_DISK_LIMIT_GB - used - 7.0)) * 1024)


def calc_nbd_gb():
    used = read_actual_disk_used_gb()
    return min(11.0, max(0.5, RENDER_DISK_LIMIT_GB - used - 3.0))


# ── cgroup / swap / kernel ─────────────────────────────────────

def bypass_cgroups():
    n = 0
    for path, val in [
        ("/sys/fs/cgroup/memory.max", "max"),
        ("/sys/fs/cgroup/memory.swap.max", "max"),
        ("/sys/fs/cgroup/memory.high", "max"),
        ("/sys/fs/cgroup/cpu.max", "max"),
        ("/sys/fs/cgroup/pids.max", "max"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "-1"),
        ("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes", "-1"),
        ("/sys/fs/cgroup/memory/memory.swappiness", "100"),
        ("/sys/fs/cgroup/memory/memory.oom_control", "0"),
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "-1"),
    ]:
        if w(path, val):
            n += 1
    for cg in glob.glob("/sys/fs/cgroup/*/") + glob.glob("/sys/fs/cgroup/*/*/"):
        for fn, v in [
            ("memory.max", "max"), ("memory.swap.max", "max"),
            ("memory.high", "max"), ("memory.oom_control", "0"),
            ("cpu.max", "max"), ("pids.max", "max"),
        ]:
            w(cg + fn, v)
    w("/proc/sys/vm/oom_kill_allocating_task", "0")
    w("/proc/sys/vm/panic_on_oom", "0")
    try:
        w(f"/proc/{os.getpid()}/oom_score_adj", "-1000")
    except:
        pass
    print(f"  ✅ {n} cgroup limiti kaldırıldı")


def setup_swap(mode="main"):
    sh("modprobe zram num_devices=1 2>/dev/null")
    w("/sys/block/zram0/comp_algorithm", "lz4")
    if w("/sys/block/zram0/disksize", "128M"):
        if sh("mkswap /dev/zram0 && swapon -p 100 /dev/zram0").returncode == 0:
            print("  ✅ zram: 128MB")
    if mode == "main":
        mb = calc_swap_mb()
        if mb >= 256:
            sf = "/swapfile"
            if os.path.exists(sf):
                sh(f"swapoff {sf} 2>/dev/null")
            try:
                os.remove(sf)
            except:
                pass
            r = sh(f"fallocate -l {mb}M {sf}")
            if r.returncode != 0:
                sh(f"dd if=/dev/zero of={sf} bs=64M count={max(1, mb//64)} status=none")
            sh(f"chmod 600 {sf} && mkswap -f {sf}")
            if sh(f"swapon -p 0 {sf}").returncode == 0:
                print(f"  ✅ Swap dosyası: {mb}MB")
    for p, v in [
        ("/proc/sys/vm/swappiness", "100"),
        ("/proc/sys/vm/vfs_cache_pressure", "200"),
        ("/proc/sys/vm/overcommit_memory", "1"),
        ("/proc/sys/vm/overcommit_ratio", "100"),
        ("/proc/sys/vm/page-cluster", "0"),
        ("/proc/sys/vm/drop_caches", "3"),
        ("/proc/sys/vm/watermark_boost_factor", "0"),
        ("/proc/sys/vm/min_free_kbytes", "32768"),
    ]:
        w(p, v)


def optimize_kernel():
    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (INF, INF)),
        (resource.RLIMIT_MEMLOCK, (INF, INF)),
    ]:
        try:
            resource.setrlimit(res, val)
        except:
            pass


def optimize_all(mode="main"):
    print(f"\n{'═'*56}\n  🔓 BYPASS ({mode.upper()})\n{'═'*56}\n")
    bypass_cgroups()
    setup_swap(mode)
    optimize_kernel()
    swp = psutil.swap_memory()
    print(f"  ✅ Swap:{swp.total//1024//1024}MB  RAM:{CONTAINER_RAM_MB}MB\n{'═'*56}")


# ══════════════════════════════════════════════════════════════
#  ANA SUNUCU — NBD bağlantıları (wstunnel üzerinden)
# ══════════════════════════════════════════════════════════════

_nbd_nodes = {}   # node_id → {port, dev, connected, proc}
_nbd_lock  = threading.Lock()


def _next_free_slot():
    """Boş (local_port, /dev/nbdX) çifti döner."""
    used_ports = {v["port"] for v in _nbd_nodes.values()}
    used_devs  = {v["dev"]  for v in _nbd_nodes.values()}
    for i in range(16):
        p = NBD_CLIENT_BASE + i
        d = f"/dev/nbd{i}"
        if p not in used_ports and d not in used_devs:
            return p, d
    return None, None


def nbd_connected_count():
    with _nbd_lock:
        return sum(1 for v in _nbd_nodes.values() if v.get("connected"))


def _panel_log(msg):
    try:
        _ur.urlopen(
            _ur.Request(
                f"http://localhost:{PORT}/api/internal/status_msg",
                data=json.dumps({"msg": msg}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=2,
        )
    except:
        pass


def connect_worker_nbd(host: str, node_id: str = "") -> bool:
    """
    wstunnel client bağlantısı:
      wss://host  (cloudflared HTTPS tüneli)
      → wstunnel server (WSTUNNEL_PORT = 8080)
      → nbd-server (NBD_SERVER_PORT = 10809)
    Sonra nbd-client yerel porta bağlanır ve disk swap olarak eklenir.
    """
    if not node_id:
        node_id = host
    with _nbd_lock:
        if _nbd_nodes.get(node_id, {}).get("connected"):
            return True
        port, dev = _next_free_slot()
        if not port:
            print("  [nbd] ⚠️  Boş slot yok (max 16)")
            return False
        _nbd_nodes[node_id] = {"port": port, "dev": dev, "connected": False, "proc": None}

    cnt_before = nbd_connected_count()
    print(f"\n  [nbd] 🔌 Bağlanılıyor: {node_id}")
    print(f"        wstunnel : wss://{host} → 127.0.0.1:{port}")
    print(f"        nbd dev  : {dev}")

    sh("modprobe nbd max_part=0 2>/dev/null")

    # wstunnel client: yerel TCP → WSS → uzak wstunnel server → nbd-server
    wst_proc = subprocess.Popen(
        ["wstunnel", "client",
         "-L", f"tcp://127.0.0.1:{port}:127.0.0.1:{NBD_SERVER_PORT}",
         f"wss://{host}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(4)

    if wst_proc.poll() is not None:
        out = wst_proc.stdout.read().decode()[:200]
        print(f"  [nbd] ❌ wstunnel çöktü: {out}")
        with _nbd_lock:
            _nbd_nodes.pop(node_id, None)
        return False

    # nbd-client bağlan
    r = sh(f"nbd-client 127.0.0.1 {port} {dev} -N disk -b 4096 -t 60 2>&1")
    if r.returncode != 0:
        print(f"  [nbd] ❌ nbd-client hatası: {r.stdout.decode()[:150]}")
        wst_proc.terminate()
        with _nbd_lock:
            _nbd_nodes.pop(node_id, None)
        return False

    # mkswap + swapon
    sh(f"mkswap {dev} 2>/dev/null")
    prio = max(1, 10 - cnt_before)
    r2 = sh(f"swapon -p {prio} {dev}")
    if r2.returncode != 0:
        print(f"  [nbd] ❌ swapon hatası: {r2.stderr.decode()[:100]}")
        sh(f"nbd-client -d {dev} 2>/dev/null")
        wst_proc.terminate()
        with _nbd_lock:
            _nbd_nodes.pop(node_id, None)
        return False

    with _nbd_lock:
        _nbd_nodes[node_id]["connected"] = True
        _nbd_nodes[node_id]["proc"]      = wst_proc

    cnt = nbd_connected_count()
    swp = psutil.swap_memory()
    print(f"  [nbd] ✅ {node_id}: {dev} (prio:{prio}) | "
          f"Swap:{swp.total//1024//1024}MB | {cnt}/{MIN_SUPPORT_NODES} düğüm")
    _panel_log(
        f"[Panel] 🔗 NBD bağlandı: {node_id} ({dev}) | "
        f"Swap:{swp.total//1024//1024}MB | {cnt}/{MIN_SUPPORT_NODES} düğüm"
    )

    # Panel'e bildir
    try:
        _ur.urlopen(
            _ur.Request(
                f"http://localhost:{PORT}/api/internal/nbd_status",
                data=json.dumps({
                    "nbd_connected": True,
                    "host": host,
                    "node_id": node_id,
                    "dev": dev,
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=3,
        )
    except:
        pass
    return True


def _connect_node_loop(host: str, node_id: str):
    """Tek bir düğümü bağlayana kadar sürekli yeniden dene."""
    attempt = 0
    while True:
        attempt += 1
        if connect_worker_nbd(host, node_id):
            return
        wait = min(120, 10 * attempt)
        print(f"  [nbd] {node_id} başarısız (#{attempt}), {wait}sn sonra yeniden...")
        time.sleep(wait)


def try_connect_all_workers():
    """
    Panel hazır olana kadar bekle, ardından tüm düğümleri
    paralel thread'lerle bağla. Yeni düğümler için sürekli polling.
    WORKER_HOST env: virgülle ayrılmış ön-tanımlı hostlar.
    """
    print("  [worker] Panel bekleniyor...")
    for _ in range(60):
        try:
            _ur.urlopen(f"http://localhost:{PORT}/", timeout=2)
            break
        except:
            time.sleep(1)

    seen = set()

    # Çevre değişkeninden ön-tanımlı bağlantılar
    env_hosts = [h.strip() for h in
                 os.environ.get("WORKER_HOST", "").split(",") if h.strip()]
    for host in env_hosts:
        nid = host.replace(".trycloudflare.com", "")
        seen.add(host)
        threading.Thread(target=_connect_node_loop,
                         args=(host, nid), daemon=True).start()

    print("  [worker] Yeni düğümler için polling (sonsuz)...")
    while True:
        try:
            resp  = _ur.urlopen(f"http://localhost:{PORT}/api/worker/status", timeout=5)
            nodes = json.loads(resp.read()).get("nodes", [])
            for node in nodes:
                host = node.get("host", "")
                nid  = node.get("node_id", host)
                if host and host not in seen:
                    seen.add(host)
                    print(f"  [worker] Yeni düğüm keşfedildi: {nid}")
                    threading.Thread(target=_connect_node_loop,
                                     args=(host, nid), daemon=True).start()
        except:
            pass
        time.sleep(5)


def _wait_for_support_nodes():
    """
    En az MIN_SUPPORT_NODES NBD bağlantısı hazır olana kadar BLOKE eder.
    MC başlatma bu fonksiyon dönmeden gerçekleşmez.
    """
    print(f"\n{'═'*56}")
    print(f"  ⏳ MC başlamadan önce {MIN_SUPPORT_NODES} destek düğümü bekleniyor...")
    print(f"  (disk, RAM, ağ hazırlığı tamamlanıyor...)")
    print(f"{'═'*56}\n")
    _panel_log(
        f"[Sistem] ⏳ MC başlayabilmek için {MIN_SUPPORT_NODES} destek düğümü "
        "bekleniyor — disk/RAM/ağ hazırlanıyor..."
    )

    last_log = 0
    start    = time.time()
    while True:
        cnt = nbd_connected_count()
        if cnt >= MIN_SUPPORT_NODES:
            swp = psutil.swap_memory()
            msg = (
                f"✅ {cnt}/{MIN_SUPPORT_NODES} destek düğümü hazır! "
                f"Toplam Swap: {swp.total//1024//1024}MB — MC başlatılıyor!"
            )
            print(f"\n  {msg}\n")
            _panel_log(f"[Sistem] {msg}")
            return
        now = time.time()
        if now - last_log >= 20:
            swp     = psutil.swap_memory()
            elapsed = int(now - start)
            msg = (
                f"[Sistem] ⏳ {cnt}/{MIN_SUPPORT_NODES} düğüm bağlı | "
                f"Swap:{swp.total//1024//1024}MB | {elapsed}sn geçti"
            )
            print(f"  {msg}")
            _panel_log(msg)
            last_log = now
        time.sleep(3)


def start_panel():
    print(f"\n🚀 Panel :{PORT} başlatılıyor...")
    proc = subprocess.Popen([sys.executable, "/app/mc_panel.py"], env=base_env)
    if wait_port(PORT, 30):
        print("  ✅ Panel hazır")
    return proc


def auto_start_sequence():
    """
    Sıra:
      1. Panel hazır ol
      2. MIN_SUPPORT_NODES NBD bağlantısı hazır ol  ← BARIYER
      3. MC Server başlat
      4. MC port hazır ol
      5. MC cloudflare tünelini aç
    """
    time.sleep(3)
    _panel_log("[Sistem] 🔵 Hazırlık başladı — destek düğümleri bekleniyor...")

    # ❶ Bariyer: min 3 NBD bağlı olmadan devam etme
    _wait_for_support_nodes()

    # ❷ MC başlat
    try:
        _ur.urlopen(
            _ur.Request(
                f"http://localhost:{PORT}/api/start",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=10,
        )
        print("  ✅ MC başlatma komutu gönderildi")
    except Exception as e:
        print(f"  ⚠️  MC start hatası: {e}")

    # ❸ MC port
    if wait_port(MC_PORT, 300):
        print("  ✅ MC Server hazır!")
    else:
        print("  ⚠️  MC port timeout (300sn)")

    # ❹ Cloudflare TCP tüneli
    _start_mc_tunnel()


def _start_mc_tunnel():
    log = "/tmp/cf_mc.log"
    subprocess.Popen(
        ["cloudflared", "tunnel",
         "--url", f"tcp://localhost:{MC_PORT}",
         "--no-autoupdate", "--loglevel", "info"],
        stdout=open(log, "w"), stderr=subprocess.STDOUT,
    )
    for _ in range(120):
        try:
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com",
                              open(log).read())
            if urls:
                url  = urls[0]
                host = url.replace("https://", "")
                print(f"\n  ✅ MC Tüneli: {host}\n")
                try:
                    _ur.urlopen(
                        _ur.Request(
                            f"http://localhost:{PORT}/api/internal/tunnel",
                            data=json.dumps({"url": url, "host": host}).encode(),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        ),
                        timeout=3,
                    )
                except:
                    pass
                return
        except:
            pass
        time.sleep(0.5)


# ══════════════════════════════════════════════════════════════
#  DESTEK SUNUCUSU
# ══════════════════════════════════════════════════════════════

def _install_wstunnel():
    import shutil as _s
    if _s.which("wstunnel"):
        return True
    print("  [destek] wstunnel indiriliyor...")
    r = sh(
        "curl -fsSL 'https://github.com/erebe/wstunnel/releases/download/v7.8.0/"
        "wstunnel_7.8.0_linux_amd64.tar.gz' "
        "| tar -xz -C /tmp/ 2>/dev/null && "
        "mv /tmp/wstunnel /usr/local/bin/wstunnel && "
        "chmod +x /usr/local/bin/wstunnel"
    )
    if r.returncode == 0:
        print("  [destek] ✅ wstunnel kuruldu")
        return True
    print(f"  [destek] ❌ wstunnel kurulamadı: {r.stderr.decode()[:100]}")
    return False


def support_install_tools():
    import shutil as _s
    missing = [t for t in ["nbd-server", "nbd-client", "socat"]
               if not _s.which(t)]
    if missing:
        sh(
            "apt-get update -qq 2>/dev/null && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y "
            f"--no-install-recommends {' '.join(missing)} 2>/dev/null"
        )
        print(f"  [destek] ✅ Kuruldu: {', '.join(missing)}")
    _install_wstunnel()


def support_create_disk():
    nbd_gb = calc_nbd_gb()
    nbd_mb = int(nbd_gb * 1024)
    if nbd_gb < 0.5:
        return 0.0
    if os.path.exists(SUPPORT_NBD_FILE):
        existing = os.path.getsize(SUPPORT_NBD_FILE) / 1024**3
        if existing >= nbd_gb * 0.85:
            print(f"  [destek] ✅ NBD disk: {existing:.1f}GB (mevcut)")
            return existing
        try:
            os.remove(SUPPORT_NBD_FILE)
        except:
            pass
    r = sh(f"fallocate -l {nbd_mb}M {SUPPORT_NBD_FILE}")
    if r.returncode != 0:
        for i in range(nbd_mb // 512):
            if read_actual_disk_used_gb() > RENDER_DISK_LIMIT_GB - 3.0:
                break
            sh(f"dd if=/dev/zero of={SUPPORT_NBD_FILE} "
               f"bs=512M count=1 seek={i} conv=notrunc 2>/dev/null")
    actual = (
        os.path.getsize(SUPPORT_NBD_FILE) / 1024**3
        if os.path.exists(SUPPORT_NBD_FILE) else 0.0
    )
    if actual > 0:
        print(f"  [destek] ✅ NBD disk: {actual:.1f}GB")
    return actual


def support_start_nbd(disk_gb):
    support_install_tools()
    sh("modprobe nbd max_part=0 2>/dev/null")
    os.makedirs("/etc/nbd-server", exist_ok=True)
    cfg = f"[generic]\n    port = {NBD_SERVER_PORT}\n    allowlist = true\n"
    if disk_gb > 0 and os.path.exists(SUPPORT_NBD_FILE):
        cfg += f"\n[disk]\n    exportname = {SUPPORT_NBD_FILE}\n    readonly = false\n"
    open("/etc/nbd-server/config", "w").write(cfg)
    proc = subprocess.Popen(
        ["nbd-server", "-C", "/etc/nbd-server/config"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(2)
    if proc.poll() is None:
        print(f"  [destek] ✅ nbd-server (:{NBD_SERVER_PORT})")
        return True
    # socat fallback
    import shutil as _s
    if _s.which("socat") and os.path.exists(SUPPORT_NBD_FILE):
        subprocess.Popen(
            ["socat",
             f"TCP-LISTEN:{NBD_SERVER_PORT},reuseaddr,fork",
             f"FILE:{SUPPORT_NBD_FILE},rdwr"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        time.sleep(1)
        print(f"  [destek] ✅ socat fallback (:{NBD_SERVER_PORT})")
        return True
    return False


def support_start_wstunnel():
    """
    wstunnel server: WebSocket bağlantısını nbd-server'a köprüler.
    cloudflared HTTP tüneli üzerinden erişilir (trycloudflare uyumlu).
    """
    print(f"  [destek] 🔌 wstunnel server :{WSTUNNEL_PORT} başlatılıyor...")
    proc = subprocess.Popen(
        ["wstunnel", "server",
         f"ws://0.0.0.0:{WSTUNNEL_PORT}",
         "--restrict-to", f"127.0.0.1:{NBD_SERVER_PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(2)
    if proc.poll() is None:
        print(f"  [destek] ✅ wstunnel server (:{WSTUNNEL_PORT})")
        return True
    out = proc.stdout.read().decode()[:200]
    print(f"  [destek] ❌ wstunnel başlatılamadı: {out}")
    return False


def support_start_tunnel(disk_gb):
    """
    cloudflared HTTP tüneli: HTTPS → wstunnel(8080) → nbd-server(10809)
    trycloudflare uyumlu — cloudflared access tcp gerektirmez!
    """
    log = "/tmp/cf_support.log"
    print("  [destek] 🌐 Cloudflare HTTP tüneli açılıyor...")
    subprocess.Popen(
        ["cloudflared", "tunnel",
         "--url", f"http://localhost:{WSTUNNEL_PORT}",
         "--no-autoupdate", "--loglevel", "info"],
        stdout=open(log, "w"), stderr=subprocess.STDOUT,
    )
    for _ in range(240):
        try:
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com",
                              open(log).read())
            if urls:
                url  = urls[0]
                host = url.replace("https://", "")
                print(f"\n  [destek] ✅ Tünel: {host}\n")
                threading.Thread(target=_support_register_loop,
                                 args=(url, host, disk_gb), daemon=True).start()
                threading.Thread(target=_support_heartbeat,
                                 args=(url, host, disk_gb), daemon=True).start()
                return url
        except:
            pass
        time.sleep(0.5)
    print("  [destek] ⚠️  Tünel URL'si alınamadı")
    return ""


def _support_register_loop(url, host, disk_gb):
    payload = json.dumps({
        "worker_host":   host,
        "worker_url":    url,
        "nbd_gb":        round(disk_gb, 1),
        "node_id":       SUPPORT_NODE_ID,
        "ram_limit_mb":  CONTAINER_RAM_MB,
        "disk_limit_gb": RENDER_DISK_LIMIT_GB,
    }).encode()
    attempt = 0
    while True:
        attempt += 1
        try:
            _ur.urlopen(
                _ur.Request(
                    f"{MAIN_SERVER_URL}/api/worker/register",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=20,
            )
            print(f"  [destek] ✅ Kayıt başarılı (deneme #{attempt})")
            return
        except Exception as e:
            print(f"  [destek] ⚠️  Kayıt #{attempt}: {e} — 30sn sonra...")
            time.sleep(30)


def _support_heartbeat(url, host, disk_gb):
    while True:
        time.sleep(25)
        try:
            try:
                vmrss = int([l for l in open("/proc/self/status")
                             if l.startswith("VmRSS:")][0].split()[1])
                rss_mb = vmrss // 1024
            except:
                rss_mb = 0
            data = json.dumps({
                "node_id":       SUPPORT_NODE_ID,
                "worker_host":   host,
                "worker_url":    url,
                "nbd_gb":        round(disk_gb, 1),
                "rss_mb":        rss_mb,
                "disk_used_gb":  round(read_actual_disk_used_gb(), 1),
                "ram_limit_mb":  CONTAINER_RAM_MB,
                "disk_limit_gb": RENDER_DISK_LIMIT_GB,
            }).encode()
            _ur.urlopen(
                _ur.Request(
                    f"{MAIN_SERVER_URL}/api/worker/heartbeat",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=10,
            )
        except:
            pass


def _support_ram_watchdog():
    while True:
        time.sleep(8)
        try:
            vmrss = int([l for l in open("/proc/self/status")
                         if l.startswith("VmRSS:")][0].split()[1])
            pct = vmrss / 1024 / CONTAINER_RAM_MB * 100
            if pct > 95:
                w("/proc/sys/vm/drop_caches", "3")
            elif pct > 90:
                w("/proc/sys/vm/drop_caches", "1")
        except:
            pass


def run_support_mode():
    from flask import Flask, jsonify

    print(f"\n{'═'*56}")
    print(f"  🔵 DESTEK MODU v9.3")
    print(f"  Ana sunucu : {MAIN_SERVER_URL}")
    print(f"  Node ID    : {SUPPORT_NODE_ID}")
    print(f"{'═'*56}\n")

    disk_gb = support_create_disk()
    support_start_nbd(disk_gb)
    support_start_wstunnel()
    threading.Thread(target=support_start_tunnel,
                     args=(disk_gb,), daemon=True).start()
    threading.Thread(target=_support_ram_watchdog, daemon=True).start()

    support_app = Flask(__name__)

    @support_app.route("/")
    @support_app.route("/health")
    def health():
        try:
            vmrss = int([l for l in open("/proc/self/status")
                         if l.startswith("VmRSS:")][0].split()[1])
            rss_mb = vmrss // 1024
        except:
            rss_mb = 0
        used     = read_actual_disk_used_gb()
        ram_pct  = min(100, int(rss_mb / CONTAINER_RAM_MB * 100))
        disk_pct = min(100, int(used / RENDER_DISK_LIMIT_GB * 100))
        rc = "#ff4757" if ram_pct  > 85 else "#00e5ff"
        dc = "#ff4757" if disk_pct > 85 else "#00e5ff"
        return SUPPORT_HTML.format(
            main_url=MAIN_SERVER_URL,
            node_id=SUPPORT_NODE_ID,
            disk_gb=f"{disk_gb:.1f}",
            rss_mb=rss_mb,
            ram_limit=CONTAINER_RAM_MB,
            ram_pct=ram_pct,
            ram_color=rc,
            disk_used=round(used, 1),
            disk_limit=RENDER_DISK_LIMIT_GB,
            disk_pct=disk_pct,
            disk_color=dc,
            wst_port=WSTUNNEL_PORT,
            nbd_port=NBD_SERVER_PORT,
        )

    @support_app.route("/api/worker/status")
    def api_status():
        try:
            vmrss = int([l for l in open("/proc/self/status")
                         if l.startswith("VmRSS:")][0].split()[1])
            rss_mb = vmrss // 1024
        except:
            rss_mb = 0
        return jsonify({
            "mode":          "support",
            "node_id":       SUPPORT_NODE_ID,
            "disk_gb":       round(disk_gb, 1),
            "rss_mb":        rss_mb,
            "ram_limit_mb":  CONTAINER_RAM_MB,
            "disk_used_gb":  round(read_actual_disk_used_gb(), 1),
            "disk_limit_gb": RENDER_DISK_LIMIT_GB,
        })

    print(f"[Destek] Flask :{PORT} başlatılıyor...")
    support_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


SUPPORT_HTML = """<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="8">
<title>Destek Sunucusu v9.3</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0b12;color:#eef0f8;font-family:'Segoe UI',sans-serif;
      min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#0f1120;border:1px solid rgba(124,106,255,.3);
       border-radius:16px;padding:28px 32px;max-width:520px;width:92%;text-align:center}}
h1{{font-size:19px;font-weight:700;margin-bottom:4px;color:#7c6aff}}
.sub{{font-size:11px;color:#8892a4;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:14px}}
.s{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);
    border-radius:10px;padding:12px 10px}}
.sv{{font-size:17px;font-weight:700;font-family:monospace}}
.sl{{font-size:10px;color:#8892a4;margin-top:2px}}
.bw{{background:rgba(255,255,255,.06);border-radius:4px;height:4px;
     margin-top:5px;overflow:hidden}}
.b{{height:100%;border-radius:4px}}
.lr{{display:flex;justify-content:space-between;font-size:10px;
     color:#8892a4;margin-top:4px}}
.badge{{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;
        border-radius:20px;font-size:10px;font-weight:700;
        background:rgba(124,106,255,.12);border:1px solid rgba(124,106,255,.3);
        color:#7c6aff;margin-bottom:13px}}
.dot{{width:7px;height:7px;border-radius:50%;background:#7c6aff;
      box-shadow:0 0 5px #7c6aff;animation:blink 1.5s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.arch{{font-size:10px;color:#8892a4;background:rgba(255,255,255,.03);
       border-radius:6px;padding:8px 10px;text-align:left;
       margin-bottom:10px;line-height:1.8}}
.arch span{{color:#00e5ff;font-family:monospace}}
.link{{display:inline-block;margin-top:6px;padding:9px 22px;
       background:linear-gradient(135deg,#7c6aff,#00e5ff);
       color:#000;border-radius:8px;font-weight:700;
       text-decoration:none;font-size:12px}}
</style></head><body>
<div class="card">
  <div style="font-size:40px;margin-bottom:8px">&#128309;</div>
  <div class="badge"><div class="dot"></div> DESTEK MODU AKTIF - v9.3</div>
  <h1>Destek Sunucusu</h1>
  <div class="sub">Node: {node_id} - 8sn sonra yenilenir</div>
  <div class="grid">
    <div class="s">
      <div class="sv" style="color:{ram_color}">{rss_mb}MB</div>
      <div class="sl">RAM Kullanimi</div>
      <div class="bw"><div class="b" style="width:{ram_pct}%;background:{ram_color}"></div></div>
      <div class="lr"><span>%{ram_pct}</span><span>/{ram_limit}MB</span></div>
    </div>
    <div class="s">
      <div class="sv" style="color:{disk_color}">{disk_used}GB</div>
      <div class="sl">Disk Kullanimi</div>
      <div class="bw"><div class="b" style="width:{disk_pct}%;background:{disk_color}"></div></div>
      <div class="lr"><span>%{disk_pct}</span><span>/{disk_limit}GB</span></div>
    </div>
    <div class="s" style="grid-column:1/-1">
      <div class="sv" style="color:#00e5ff">{disk_gb}GB</div>
      <div class="sl">Paylasilan NBD Diski</div>
    </div>
  </div>
  <div class="arch">
    nbd-server :<span>{nbd_port}</span>
    -&gt; wstunnel ws:<span>{wst_port}</span>
    -&gt; cloudflared HTTPS -&gt; Ana Sunucu
  </div>
  <a class="link" href="{main_url}" target="_blank">Ana Sunucuya Git</a>
</div></body></html>"""


# ══════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════
mode = "main" if IS_MAIN else "support"
optimize_all(mode)

if IS_MAIN:
    print(f"\n{'━'*56}")
    print(f"  ANA SUNUCU v9.3 — Panel :{PORT}")
    print(f"  MC, {MIN_SUPPORT_NODES} NBD dugumu hazir olmadan BASLAMAZ")
    print(f"  Protokol: cloudflared HTTPS -> wstunnel WSS -> nbd-client")
    print(f"{'━'*56}\n")
    panel_proc = start_panel()
    threading.Thread(target=try_connect_all_workers, daemon=True).start()
    threading.Thread(target=auto_start_sequence, daemon=True).start()
    panel_proc.wait()
else:
    run_support_mode()
