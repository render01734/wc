"""
⛏️  Minecraft Server Boot — RENDER BYPASS v6.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OTOMATİK MOD TESPİTİ:
  - https://wc-tsgd.onrender.com → ANA SUNUCU  (Minecraft Panel)
  - Diğer tüm URL'ler           → DESTEK MODU  (Disk/RAM sağlayıcı)

DESTEK MODU:
  - MC Panel açılmaz
  - 14GB blok dosya oluşturur → NBD ile paylaşır
  - Cloudflared TCP tüneli → Ana sunucuya bildirim
  - Ana sunucu bu diski swap olarak kullanır (+14GB bellek)
"""

import os, sys, subprocess, time, socket, resource, threading, re, glob

# ══════════════════════════════════════════════════════════════
#  ANA SUNUCU TESPİTİ
# ══════════════════════════════════════════════════════════════

MAIN_SERVER_URL = "https://wc-tsgd.onrender.com"

# Render bu env var'ı otomatik olarak set eder (örn: https://mc-worker-abc.onrender.com)
MY_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/").rstrip("/")

# FORCE_MAIN=1 ile her zaman ana sunucu modunda çalıştırılabilir
IS_MAIN = (
    MY_URL == MAIN_SERVER_URL
    or MY_URL == ""
    or os.environ.get("FORCE_MAIN", "") == "1"
)

print("\n" + "━"*52)
print("  ⛏️   Minecraft Server — RENDER BYPASS v6.0")
print(f"      URL: {MY_URL or '(belirlenemedi)'}")
print(f"      MOD: {'🟢 ANA SUNUCU' if IS_MAIN else '🔵 DESTEK SUNUCUSU'}")
print("━"*52 + "\n")

PORT    = int(os.environ.get("PORT", "5000"))
MC_PORT = 25565
MC_RAM  = os.environ.get("MC_RAM", "2G")

base_env = {
    **os.environ,
    "HOME": "/root", "USER": "root", "LOGNAME": "root",
    "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
    "JAVA_HOME": "/usr/lib/jvm/java-21-openjdk-amd64",
    "PATH": "/usr/lib/jvm/java-21-openjdk-amd64/bin"
            ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "MC_RAM": MC_RAM,
    "PORT":   str(PORT),
}

INF = resource.RLIM_INFINITY


def w(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except Exception:
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


# ══════════════════════════════════════════════════════════════
#  ORTAK: CGROUP BYPASS + SWAP + KERNEL OPTİMİZASYONU
# ══════════════════════════════════════════════════════════════

def bypass_cgroups():
    print("  [cgroup] Limitler kaldırılıyor...")
    n = 0
    for path, val in [
        ("/sys/fs/cgroup/memory.max",      "max"),
        ("/sys/fs/cgroup/memory.swap.max", "max"),
        ("/sys/fs/cgroup/memory.high",     "max"),
        ("/sys/fs/cgroup/cpu.max",         "max"),
        ("/sys/fs/cgroup/pids.max",        "max"),
    ]:
        if w(path, val): n += 1

    for cg in glob.glob("/sys/fs/cgroup/*/") + glob.glob("/sys/fs/cgroup/*/*/"):
        for fn, v in [("memory.max","max"),("memory.swap.max","max"),
                      ("memory.high","max"),("cpu.max","max"),("pids.max","max")]:
            w(cg + fn, v)

    for path, val in [
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes",       "-1"),
        ("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes", "-1"),
        ("/sys/fs/cgroup/memory/memory.soft_limit_in_bytes",  "-1"),
        ("/sys/fs/cgroup/memory/memory.swappiness",           "100"),
        ("/sys/fs/cgroup/memory/memory.oom_control",          "0"),
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us",               "-1"),
        ("/sys/fs/cgroup/pids/pids.max",                      "max"),
    ]:
        if w(path, val): n += 1

    for cg in glob.glob("/sys/fs/cgroup/memory/*/"):
        w(cg + "memory.limit_in_bytes",       "-1")
        w(cg + "memory.memsw.limit_in_bytes", "-1")
        w(cg + "memory.swappiness",           "100")
        w(cg + "memory.oom_control",          "0")

    w("/proc/sys/vm/oom_kill_allocating_task", "0")
    w("/proc/sys/vm/panic_on_oom",             "0")
    try:
        w(f"/proc/{os.getpid()}/oom_score_adj", "-1000")
    except Exception:
        pass

    print(f"  ✅ cgroup → {n} limit kaldırıldı")


def setup_swap():
    print("  [swap] Disk → Swap dönüşümü...")
    import psutil

    disk     = psutil.disk_usage("/")
    free_gb  = disk.free / 1024 / 1024 / 1024
    swap_gb  = min(8, int(free_gb * 0.50))
    swap_gb  = max(2, swap_gb)
    swap_mb  = swap_gb * 1024
    swap_file = "/swapfile"

    swp = psutil.swap_memory()
    if swp.total >= swap_mb * 1024 * 1024 * 0.8:
        print(f"  ✅ Swap zaten aktif: {swp.total//1024//1024}MB")
    else:
        print(f"  📊 Disk boş: {free_gb:.1f}GB → Swap: {swap_gb}GB oluşturuluyor...")

        if os.path.exists(swap_file):
            sh(f"swapoff {swap_file} 2>/dev/null")
            try: os.remove(swap_file)
            except: pass

        ret = sh(f"fallocate -l {swap_mb}M {swap_file}")
        if ret.returncode != 0:
            sh(f"dd if=/dev/zero of={swap_file} bs=64M count={max(1,swap_mb//64)} status=none")

        sh(f"chmod 600 {swap_file}")
        sh(f"mkswap -f {swap_file}")
        ret2 = sh(f"swapon -p 0 {swap_file}")

        if ret2.returncode == 0:
            print(f"  ✅ Swap dosyası aktif: {swap_mb}MB")
        else:
            print(f"  ⚠️  swapon: {ret2.stderr.decode().strip()}")

    # zram
    sh("modprobe zram num_devices=1 2>/dev/null")
    mem_mb  = psutil.virtual_memory().total // 1024 // 1024
    zram_mb = min(2048, mem_mb // 2)
    w("/sys/block/zram0/comp_algorithm", "lz4")
    if w("/sys/block/zram0/disksize", f"{zram_mb}M"):
        if sh("mkswap /dev/zram0 && swapon -p 100 /dev/zram0").returncode == 0:
            print(f"  ✅ zram: {zram_mb}MB sıkıştırılmış RAM")

    for path, val in [
        ("/proc/sys/vm/swappiness",             "100"),
        ("/proc/sys/vm/vfs_cache_pressure",     "200"),
        ("/proc/sys/vm/overcommit_memory",      "1"),
        ("/proc/sys/vm/overcommit_ratio",       "100"),
        ("/proc/sys/vm/page-cluster",           "0"),
        ("/proc/sys/vm/drop_caches",            "3"),
        ("/proc/sys/vm/watermark_boost_factor", "0"),
    ]:
        w(path, val)

    swp2 = psutil.swap_memory()
    mem  = psutil.virtual_memory()
    total_mb = (mem.total + swp2.total) // 1024 // 1024
    print(f"  🎯 RAM={mem.total//1024//1024}MB + Swap={swp2.total//1024//1024}MB = {total_mb}MB")
    return total_mb


def optimize_kernel():
    print("  [kernel] Parametreler ayarlanıyor...")
    params = {
        "/proc/sys/kernel/pid_max":                  "4194304",
        "/proc/sys/kernel/threads-max":              "4194304",
        "/proc/sys/kernel/sched_rt_runtime_us":      "-1",
        "/proc/sys/fs/file-max":                     "2097152",
        "/proc/sys/fs/nr_open":                      "2097152",
        "/proc/sys/net/core/somaxconn":              "65535",
        "/proc/sys/net/ipv4/tcp_tw_reuse":           "1",
        "/proc/sys/net/ipv4/tcp_fin_timeout":        "10",
    }
    ok = sum(w(p, v) for p, v in params.items())

    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (INF, INF)),
        (resource.RLIMIT_STACK,   (INF, INF)),
        (resource.RLIMIT_MEMLOCK, (INF, INF)),
    ]:
        try: resource.setrlimit(res, val)
        except Exception: pass

    w("/sys/kernel/mm/transparent_hugepage/enabled", "madvise")
    print(f"  ✅ {ok}/{len(params)} parametre ayarlandı")


def optimize_all():
    print("\n" + "═"*52)
    print("  🔓 RENDER BYPASS — TÜM LİMİTLER KALDIRILIYOR")
    print("═"*52 + "\n")

    bypass_cgroups()
    print()
    total_mb = setup_swap()
    print()
    optimize_kernel()

    import psutil
    mem  = psutil.virtual_memory()
    swp  = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    print("\n" + "═"*52)
    print(f"  CPU     : {psutil.cpu_count()} çekirdek")
    print(f"  RAM     : {mem.total//1024//1024} MB")
    print(f"  Swap    : {swp.total//1024//1024} MB")
    print(f"  Disk    : kullanılan={disk.used//1024//1024//1024}GB / limit≈18GB")
    print(f"  TOPLAM  : {(mem.total+swp.total)//1024//1024} MB kullanılabilir")
    print("═"*52)
    return total_mb


# ══════════════════════════════════════════════════════════════
#  ANA SUNUCU: Panel + MC + Tunnel
# ══════════════════════════════════════════════════════════════

_worker_registered = threading.Event()
_worker_info       = {}


def start_panel():
    print(f"\n🚀 [2/4] Panel başlatılıyor (:{PORT})...")
    proc = subprocess.Popen(
        [sys.executable, "/app/mc_panel.py"],
        env=base_env,
    )
    if wait_port(PORT, 30):
        print(f"  ✅ Panel hazır → http://0.0.0.0:{PORT}")
    else:
        print("  ⚠️  Panel başlatılıyor...")
    return proc


def auto_start_sequence():
    time.sleep(2)
    print("\n⛏️  [3/4] Minecraft Server başlatılıyor...")
    try:
        import urllib.request, json
        req = urllib.request.Request(
            f"http://localhost:{PORT}/api/start",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        print("  ✅ MC başlatma komutu gönderildi")
    except Exception as e:
        print(f"  ⚠️  {e}")

    print("  ⏳ MC Server bekleniyor (max 5 dk)...")
    if wait_port(MC_PORT, 300):
        print("  ✅ MC Server hazır!")
    else:
        print("  ⚠️  MC portu zaman aşımı")

    print("\n🌐 [4/4] Cloudflare Tunnel...")
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
                import json as _j
                tunnel_url = urls[0]
                host = tunnel_url.replace("https://", "")
                print(f"\n  ┌──────────────────────────────────────────┐")
                print(f"  │  ✅ MC Sunucu Adresi:                     │")
                print(f"  │  📌 {host:<40}│")
                print(f"  └──────────────────────────────────────────┘\n")
                try:
                    data = _j.dumps({"url": tunnel_url, "host": host}).encode()
                    req2 = urllib.request.Request(
                        f"http://localhost:{PORT}/api/internal/tunnel",
                        data=data,
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    urllib.request.urlopen(req2, timeout=3)
                except Exception:
                    pass
                return
        except Exception:
            pass
        time.sleep(0.5)
    print("  ⚠️  Tunnel URL alınamadı")


def connect_worker_nbd(host: str, local_port: int = 10810, nbd_dev: str = "/dev/nbd0"):
    print(f"\n  [worker-nbd] Bağlanılıyor: {host}...")
    sh("modprobe nbd max_part=0 2>/dev/null")

    cf_proc = subprocess.Popen([
        "cloudflared", "access", "tcp",
        "--hostname", host,
        "--url", f"localhost:{local_port}",
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(4)

    ret = sh(f"nbd-client localhost {local_port} {nbd_dev} -N disk -b 4096 -t 60")
    if ret.returncode != 0:
        print(f"  ⚠️  nbd-client başarısız: {ret.stderr.decode().strip()}")
        cf_proc.terminate()
        return False

    sh(f"mkswap {nbd_dev}")
    ret2 = sh(f"swapon -p 5 {nbd_dev}")
    if ret2.returncode == 0:
        import psutil as _p
        swp = _p.swap_memory()
        mem = _p.virtual_memory()
        print(f"  ✅ Worker diski swap'a eklendi!")
        print(f"  🎯 YENİ TOPLAM: RAM={mem.total//1024//1024}MB"
              f" + Swap={swp.total//1024//1024}MB"
              f" = {(mem.total+swp.total)//1024//1024}MB")
        return True
    else:
        print(f"  ⚠️  swapon worker: {ret2.stderr.decode().strip()}")
        return False


def try_connect_worker():
    host = os.environ.get("WORKER_HOST", "").strip()
    if host:
        print(f"  [worker] WORKER_HOST env: {host}")
        connect_worker_nbd(host)
        return
    print("  [worker] WORKER_HOST bekleniyor (90sn timeout)...")
    if _worker_registered.wait(timeout=90):
        host = _worker_info.get("worker_host", "")
        if host:
            connect_worker_nbd(host)
    else:
        print("  [worker] Timeout — worker yok, lokal swap ile devam")


# ══════════════════════════════════════════════════════════════
#  DESTEK SUNUCUSU: Disk Paylaşımı
# ══════════════════════════════════════════════════════════════

SUPPORT_NBD_GB   = 14
SUPPORT_NBD_PORT = 10809
SUPPORT_NBD_FILE = "/nbd_disk.img"
SUPPORT_NODE_ID  = MY_URL.replace("https://", "").replace(".onrender.com", "")


def support_create_disk():
    size_mb = SUPPORT_NBD_GB * 1024
    if os.path.exists(SUPPORT_NBD_FILE):
        gb = os.path.getsize(SUPPORT_NBD_FILE) / 1024**3
        if gb >= SUPPORT_NBD_GB * 0.9:
            print(f"  [destek] ✅ Blok dosya zaten var: {gb:.1f}GB")
            return
    print(f"  [destek] 💾 {SUPPORT_NBD_GB}GB blok dosya oluşturuluyor...")
    ret = sh(f"fallocate -l {size_mb}M {SUPPORT_NBD_FILE}")
    if ret.returncode != 0:
        for i in range(SUPPORT_NBD_GB):
            sh(f"dd if=/dev/zero of={SUPPORT_NBD_FILE} bs=64M count=16 seek={i*1024} 2>/dev/null")
            print(f"  [destek] {i+1}/{SUPPORT_NBD_GB}GB...")
    gb = os.path.getsize(SUPPORT_NBD_FILE) / 1024**3
    print(f"  [destek] ✅ Blok dosya hazır: {gb:.1f}GB")


def support_start_nbd():
    sh("modprobe nbd max_part=0 2>/dev/null")

    # ── nbd-server dene ────────────────────────────────────────
    import shutil as _shutil
    if _shutil.which("nbd-server"):
        try:
            os.makedirs("/etc/nbd-server", exist_ok=True)
            open("/etc/nbd-server/config", "w").write(f"""
[generic]
    port = {SUPPORT_NBD_PORT}
    allowlist = true
[disk]
    exportname = {SUPPORT_NBD_FILE}
    readonly = false
    flush = true
""")
            print(f"  [destek] 🔌 nbd-server başlatılıyor...")
            proc = subprocess.Popen(
                ["nbd-server", "-C", "/etc/nbd-server/config"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            time.sleep(2)
            if proc.poll() is None:
                print(f"  [destek] ✅ nbd-server aktif (port {SUPPORT_NBD_PORT})")
                return True
        except Exception as e:
            print(f"  [destek] ⚠️  nbd-server başlatma hatası: {e}")
    else:
        print("  [destek] ℹ️  nbd-server kurulu değil → socat ile devam")

    # ── socat fallback (raw TCP köprüsü) ──────────────────────
    if _shutil.which("socat"):
        try:
            subprocess.Popen([
                "socat",
                f"TCP-LISTEN:{SUPPORT_NBD_PORT},reuseaddr,fork",
                f"FILE:{SUPPORT_NBD_FILE},rdwr"
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            time.sleep(1)
            print(f"  [destek] ✅ socat TCP köprüsü aktif (port {SUPPORT_NBD_PORT})")
            return True
        except Exception as e:
            print(f"  [destek] ⚠️  socat hatası: {e}")
    else:
        print("  [destek] ℹ️  socat kurulu değil → Python TCP sunucusu kullanılıyor")

    # ── Pure-Python fallback (hiçbir araç yoksa) ──────────────
    def _py_tcp_server():
        import socketserver, struct
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with open(SUPPORT_NBD_FILE, "r+b") as f:
                        conn = self.request
                        conn.settimeout(60)
                        while True:
                            hdr = conn.recv(8)
                            if not hdr or len(hdr) < 8:
                                break
                            cmd, offset, length = struct.unpack(">BIH", hdr[:7])
                            if cmd == 0:   # read
                                f.seek(offset)
                                conn.sendall(f.read(length))
                            elif cmd == 1: # write
                                data = conn.recv(length)
                                f.seek(offset)
                                f.write(data)
                except Exception:
                    pass
        server = socketserver.ThreadingTCPServer(("0.0.0.0", SUPPORT_NBD_PORT), Handler)
        server.allow_reuse_address = True
        print(f"  [destek] ✅ Python TCP sunucusu aktif (port {SUPPORT_NBD_PORT})")
        server.serve_forever()

    threading.Thread(target=_py_tcp_server, daemon=True).start()
    time.sleep(1)
    return True


def support_start_tunnel_and_register():
    log = "/tmp/cf_support.log"
    print(f"  [destek] 🌐 Cloudflare TCP tüneli açılıyor...")
    subprocess.Popen([
        "cloudflared", "tunnel",
        "--url", f"tcp://localhost:{SUPPORT_NBD_PORT}",
        "--no-autoupdate", "--loglevel", "info",
    ], stdout=open(log, "w"), stderr=subprocess.STDOUT)

    for _ in range(120):
        try:
            content = open(log).read()
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', content)
            if urls:
                url  = urls[0]
                host = url.replace("https://", "")
                print(f"\n  [destek] ╔══════════════════════════════════════╗")
                print(f"  [destek] ║  ✅ DESTEK SUNUCU HAZIR               ║")
                print(f"  [destek] ║  Host: {host:<30}║")
                print(f"  [destek] ╚══════════════════════════════════════╝\n")

                # Ana sunucuya kayıt ol
                _support_register(url, host)
                # Periyodik heartbeat
                threading.Thread(target=_support_heartbeat,
                                 args=(host,), daemon=True).start()
                return url
        except Exception:
            pass
        time.sleep(0.5)
    print("  [destek] ⚠️  Tünel URL alınamadı")
    return ""


def _support_register(url, host):
    import urllib.request, json as _j, psutil as _p
    try:
        mem  = _p.virtual_memory()
        disk = _p.disk_usage("/")
        data = _j.dumps({
            "worker_host": host,
            "worker_url":  url,
            "nbd_gb":      SUPPORT_NBD_GB,
            "node_id":     SUPPORT_NODE_ID,
            "ram_mb":      mem.total // 1024 // 1024,
            "disk_free_gb": disk.free // 1024 // 1024 // 1024,
        }).encode()
        req = urllib.request.Request(
            f"{MAIN_SERVER_URL}/api/worker/register",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=15)
        print(f"  [destek] ✅ Ana sunucuya kayıt tamamlandı: {MAIN_SERVER_URL}")
    except Exception as e:
        print(f"  [destek] ⚠️  Kayıt hatası: {e}")
        print(f"  [destek]    Ana sunucu: {MAIN_SERVER_URL}")


def _support_heartbeat(host):
    import urllib.request, json as _j, psutil as _p
    while True:
        time.sleep(30)
        try:
            mem  = _p.virtual_memory()
            disk = _p.disk_usage("/")
            data = _j.dumps({
                "node_id":     SUPPORT_NODE_ID,
                "ram_mb":      mem.available // 1024 // 1024,
                "disk_free_gb": disk.free // 1024 // 1024 // 1024,
            }).encode()
            req = urllib.request.Request(
                f"{MAIN_SERVER_URL}/api/worker/heartbeat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass


def run_support_mode():
    """
    DESTEK MODU — MC Panel açılmaz.
    Disk paylaşır, ana sunucuya destek verir.
    Flask sağlık endpoint'i tutar.
    """
    from flask import Flask, jsonify
    import psutil

    print("\n" + "═"*52)
    print("  🔵 DESTEK MODU — MC Panel kapalı")
    print(f"  Ana sunucu: {MAIN_SERVER_URL}")
    print("═"*52 + "\n")

    # 1. Blok dosya
    support_create_disk()
    # 2. NBD server
    support_start_nbd()
    # 3. Tünel + kayıt (arka planda)
    threading.Thread(target=support_start_tunnel_and_register, daemon=True).start()

    # 4. Sağlık paneli (Render uyuma yapmasın)
    support_app = Flask(__name__)

    @support_app.route("/")
    @support_app.route("/health")
    def health():
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        swp  = psutil.swap_memory()
        return SUPPORT_HTML.format(
            main_url=MAIN_SERVER_URL,
            node_id=SUPPORT_NODE_ID,
            nbd_gb=SUPPORT_NBD_GB,
            ram_total=mem.total//1024//1024,
            ram_free=mem.available//1024//1024,
            disk_total=disk.total//1024//1024//1024,
            disk_free=disk.free//1024//1024//1024,
            swap_total=swp.total//1024//1024,
        )

    @support_app.route("/api/worker/status")
    def status():
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return jsonify({
            "mode":    "support",
            "node_id": SUPPORT_NODE_ID,
            "main":    MAIN_SERVER_URL,
            "nbd_gb":  SUPPORT_NBD_GB,
            "ram_mb":  mem.total // 1024 // 1024,
            "disk_free_gb": disk.free // 1024 // 1024 // 1024,
        })

    print(f"[Destek] Flask sağlık paneli :{PORT}...")
    support_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


SUPPORT_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🔵 Destek Sunucusu</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #0a0b12; color: #eef0f8; font-family: 'Segoe UI', sans-serif; min-height: 100vh;
  display: flex; align-items: center; justify-content: center; }}
.card {{ background: #0f1120; border: 1px solid rgba(124,106,255,.3); border-radius: 16px;
  padding: 36px 44px; max-width: 520px; width: 90%; text-align: center; }}
.icon {{ font-size: 56px; margin-bottom: 16px; }}
h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; color: #7c6aff; }}
.sub {{ font-size: 13px; color: #8892a4; margin-bottom: 28px; }}
.stat {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }}
.s {{ background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.07);
  border-radius: 10px; padding: 14px; }}
.sv {{ font-size: 22px; font-weight: 700; color: #00e5ff; font-family: monospace; }}
.sl {{ font-size: 11px; color: #8892a4; margin-top: 3px; }}
.link {{ display: inline-block; margin-top: 8px; padding: 10px 24px;
  background: linear-gradient(135deg,#7c6aff,#00e5ff); color: #000;
  border-radius: 9px; font-weight: 700; text-decoration: none; font-size: 13px; }}
.badge {{ display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px;
  border-radius: 20px; font-size: 11px; font-weight: 700;
  background: rgba(124,106,255,.12); border: 1px solid rgba(124,106,255,.3); color: #7c6aff;
  margin-bottom: 20px; }}
.dot {{ width: 8px; height: 8px; border-radius: 50%; background: #7c6aff;
  box-shadow: 0 0 6px #7c6aff; animation: blink 1.5s infinite; }}
@keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:.3}} }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">🔵</div>
  <div class="badge"><div class="dot"></div> DESTEK MODU AKTIF</div>
  <h1>Destek Sunucusu</h1>
  <div class="sub">Bu sunucu MC Panel açmaz.<br>Ana sunucuya disk ve RAM desteği sağlar.</div>
  <div class="stat">
    <div class="s"><div class="sv">{nbd_gb}GB</div><div class="sl">💾 Paylaşılan Disk</div></div>
    <div class="s"><div class="sv">{ram_free}MB</div><div class="sl">🧠 Boş RAM</div></div>
    <div class="s"><div class="sv">{disk_free}GB</div><div class="sl">📦 Disk Boş</div></div>
    <div class="s"><div class="sv">{swap_total}MB</div><div class="sl">⚡ Swap</div></div>
  </div>
  <div style="font-size:12px;color:#3d4558;margin-bottom:16px">
    Node: <span style="color:#7c6aff;font-family:monospace">{node_id}</span>
  </div>
  <a class="link" href="{main_url}" target="_blank">→ Ana Sunucuya Git</a>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════
#  BAŞLAT
# ══════════════════════════════════════════════════════════════

print("\n⚡ Limit bypass + Optimizasyon...")
optimize_all()

if IS_MAIN:
    # ── ANA SUNUCU MODU ────────────────────────────────────────
    print(f"\n{'━'*52}")
    print(f"  🟢 ANA SUNUCU MODU")
    print(f"  Panel: http://0.0.0.0:{PORT}")
    print(f"{'━'*52}\n")

    panel_proc = start_panel()
    threading.Thread(target=auto_start_sequence, daemon=True).start()
    threading.Thread(target=try_connect_worker,  daemon=True).start()

    panel_proc.wait()
else:
    # ── DESTEK SUNUCUSU MODU ───────────────────────────────────
    run_support_mode()
