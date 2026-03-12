"""
⛏️  Minecraft Server Boot — RENDER BYPASS v10.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v10.0: NBD tamamen kaldırıldı → Resource Pool ile değiştirildi
  Destek sunucuları şunları sağlar (kernel modülü gerekmez):
    • RAM Cache   → Chunk/entity verisi JVM dışında saklanır
    • File Store  → Eski region'lar arşivlenir, disk açılır → swap büyür
    • CPU Worker  → Sıkıştırma, hash, istatistik görevleri
    • TCP Proxy   → Oyuncu bağlantı yükü dağıtılır
"""

import os, sys, subprocess, time, socket, resource, threading, re, glob, json
import psutil
import urllib.request as _ur

RENDER_DISK_LIMIT_GB = 18.0
RENDER_RAM_LIMIT_MB  = 512

MAIN_SERVER_URL = "https://wc-tsgd.onrender.com"
MY_URL  = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
IS_MAIN = (MY_URL == MAIN_SERVER_URL
           or MY_URL == ""
           or os.environ.get("FORCE_MAIN", "") == "1")
PORT    = int(os.environ.get("PORT", "5000"))
MC_PORT = 25565
MC_RAM  = os.environ.get("MC_RAM", "2G")
INF     = resource.RLIM_INFINITY


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
}

print("\n" + "━"*56)
print("  ⛏️   Minecraft Server — RENDER BYPASS v10.0")
print(f"      MOD       : {'🟢 ANA' if IS_MAIN else '🔵 AGENT'}")
print(f"      MY_URL    : {MY_URL or '(boş → ANA)'}")
print(f"      RAM       : {CONTAINER_RAM_MB}MB")
print("━"*56 + "\n")


# ─────────────────────────────────────────────
#  YARDIMCILAR
# ─────────────────────────────────────────────

def w(path, val):
    try:
        open(path, "w").write(str(val)); return True
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


def _panel_log(msg):
    try:
        _ur.urlopen(_ur.Request(
            f"http://localhost:{PORT}/api/internal/status_msg",
            data=json.dumps({"msg": msg}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=2)
    except: pass


def read_disk_used_gb():
    try:
        return 4.0 + sum(
            os.path.getsize(f) for f in ["/swapfile", "/swapfile2"]
            if os.path.exists(f)
        ) / 1024**3
    except: return 4.0


# ─────────────────────────────────────────────
#  BYPASS / SWAP / KERNEL
# ─────────────────────────────────────────────

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


def _loop_swap(sf: str, sw_mb: int, priority: int = 0) -> bool:
    """
    Garantili loop-device swap.
    Yöntem:
      1. modprobe loop (kernel modülü yükle — yoksa loop device olmaz)
      2. mknod /dev/loop0..7 (device node yoksa oluştur)
      3. losetup --find --show <dosya>  → atomik: boş device bul VE bağla
      4. mkswap + swapon
    """
    # Mevcut swap/loop temizle
    sh(f"swapoff {sf} 2>/dev/null")
    for i in range(8):
        sh(f"swapoff /dev/loop{i} 2>/dev/null")

    # Dosyayı sil + yeniden oluştur
    try:
        if os.path.exists(sf): os.remove(sf)
    except: pass
    r = sh(f"fallocate -l {sw_mb}M {sf}")
    if r.returncode != 0:
        r = sh(f"dd if=/dev/zero of={sf} bs=64M count={max(1,sw_mb//64)} status=none")
    if not os.path.exists(sf) or os.path.getsize(sf) < sw_mb * 1024 * 700:
        return False
    sh(f"chmod 600 {sf}")

    # Loop modülü yükle
    sh("modprobe loop 2>/dev/null")
    sh("modprobe loop max_loop=8 2>/dev/null")

    # /dev/loop0..7 node yoksa oluştur
    for i in range(8):
        dev = f"/dev/loop{i}"
        if not os.path.exists(dev):
            sh(f"mknod {dev} b 7 {i} 2>/dev/null")

    # losetup --find --show: boş device'ı otomatik bulur VE bağlar, path döner
    r = sh(f"losetup --find --show {sf} 2>/dev/null")
    if r.returncode == 0:
        loop_dev = r.stdout.decode().strip()
        print(f"  [swap] loop device: {loop_dev}")
    else:
        # Fallback: loop0'ı zorla temizle ve kullan
        sh("losetup -d /dev/loop0 2>/dev/null")
        r2 = sh(f"losetup /dev/loop0 {sf} 2>/dev/null")
        loop_dev = "/dev/loop0" if r2.returncode == 0 else ""

    if loop_dev:
        sh(f"mkswap -f {loop_dev}")
        r3 = sh(f"swapon -p {priority} {loop_dev}")
        if r3.returncode == 0:
            print(f"  ✅ Disk Swap: {sw_mb}MB ({loop_dev})")
            return True
        sh(f"losetup -d {loop_dev} 2>/dev/null")

    # Son çare: tmpfs-üzerinde değil dosya üzerinde dene (bazı overlay sürümlerinde çalışır)
    sh(f"mkswap -f {sf}")
    r4 = sh(f"swapon -p {priority} {sf}")
    if r4.returncode == 0:
        print(f"  ✅ Disk Swap: {sw_mb}MB (doğrudan dosya)")
        return True

    print(f"  ⚠️  Disk Swap başarısız ({sw_mb}MB): {r4.stderr.decode()[:60]}")
    return False


def setup_swap():
    # ── 1. zRAM ──────────────────────────────────────────────
    sh("modprobe zram num_devices=1 2>/dev/null")
    w("/sys/block/zram0/comp_algorithm", "lz4")
    zram_ok = False
    for zram_sz in ["512M", "384M", "256M", "128M"]:
        sh("echo 0 > /sys/block/zram0/reset 2>/dev/null")
        w("/sys/block/zram0/disksize", zram_sz)
        if sh("mkswap /dev/zram0 2>/dev/null && swapon -p 100 /dev/zram0 2>/dev/null").returncode == 0:
            print(f"  ✅ zram: {zram_sz}")
            zram_ok = True
            break
    if not zram_ok:
        print("  ⚠️  zram başlatılamadı")

    # ── 2. Disk Swap (loop-device zorunlu) ───────────────────
    swapped = False
    for sw_mb in [4096, 2048, 1024, 512]:
        if _loop_swap("/swapfile", sw_mb, priority=0):
            swapped = True
            break
    if not swapped:
        print("  ⚠️  Disk swap kurulamadı")

    for p, v in [
        ("/proc/sys/vm/swappiness",         "100"),
        ("/proc/sys/vm/vfs_cache_pressure", "200"),
        ("/proc/sys/vm/overcommit_memory",  "1"),
        ("/proc/sys/vm/overcommit_ratio",   "100"),
        ("/proc/sys/vm/page-cluster",       "0"),
        ("/proc/sys/vm/drop_caches",        "3"),
        ("/proc/sys/vm/watermark_boost_factor", "0"),
        ("/proc/sys/vm/min_free_kbytes",    "32768"),
    ]: w(p, v)


def optimize_kernel():
    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (INF, INF)),
        (resource.RLIMIT_MEMLOCK, (INF, INF)),
    ]:
        try: resource.setrlimit(res, val)
        except: pass


def optimize_all(mode="main"):
    print(f"\n{'═'*56}\n  🔓 BYPASS ({mode.upper()})\n{'═'*56}\n")
    bypass_cgroups()
    setup_swap()
    optimize_kernel()
    swp = psutil.swap_memory()
    print(f"  ✅ Swap:{swp.total//1024//1024}MB  RAM:{CONTAINER_RAM_MB}MB\n{'═'*56}")


# ─────────────────────────────────────────────
#  ANA SUNUCU — MC başlatma
# ─────────────────────────────────────────────

def start_panel():
    print(f"\n🚀 Panel :{PORT} başlatılıyor...")
    proc = subprocess.Popen([sys.executable, "/app/mc_panel.py"], env=base_env)
    if wait_port(PORT, 30):
        print("  ✅ Panel hazır")
    return proc


def auto_start_sequence():
    time.sleep(4)
    _panel_log("[Sistem] 🟢 v10.0 başladı — MC başlatılıyor...")
    try:
        _ur.urlopen(_ur.Request(
            f"http://localhost:{PORT}/api/start",
            data=json.dumps({"_internal": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=10)
        print("  ✅ MC başlatma komutu gönderildi")
    except Exception as e:
        print(f"  ⚠️  MC start hatası: {e}")

    if wait_port(MC_PORT, 300):
        print("  ✅ MC Server hazır!")
        _panel_log("[Sistem] ✅ MC Server oyuncuları bekliyor!")
    else:
        print("  ⚠️  MC port timeout (300sn)")
    _start_mc_tunnel()


def _start_mc_tunnel():
    log = "/tmp/cf_mc.log"
    subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"tcp://localhost:{MC_PORT}",
         "--no-autoupdate", "--loglevel", "info"],
        stdout=open(log, "w"), stderr=subprocess.STDOUT,
    )
    for _ in range(120):
        try:
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", open(log).read())
            if urls:
                url  = urls[0]
                host = url.replace("https://", "")
                print(f"\n  ✅ MC Tüneli: {host}\n")
                try:
                    _ur.urlopen(_ur.Request(
                        f"http://localhost:{PORT}/api/internal/tunnel",
                        data=json.dumps({"url": url, "host": host}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    ), timeout=3)
                except: pass
                return
        except: pass
        time.sleep(0.5)


# ─────────────────────────────────────────────
#  AGENT MODU (eski destek sunucusu yerine)
# ─────────────────────────────────────────────

def run_agent_mode():
    agent_path = "/app/agent.py"
    ram_cache_mb = max(200, CONTAINER_RAM_MB - 150)

    print(f"\n{'═'*56}")
    print(f"  🔵 AGENT MODU v10.0")
    print(f"  Ana sunucu : {MAIN_SERVER_URL}")
    print(f"  RAM Cache  : {ram_cache_mb}MB")
    print(f"{'═'*56}\n")

    if not os.path.exists(agent_path):
        print("  [agent] ⚠️  /app/agent.py bulunamadı!")
        # Basit sağlık sunucusu
        from flask import Flask, jsonify
        app2 = Flask(__name__)
        @app2.route("/")
        @app2.route("/health")
        def h():
            return jsonify({"status": "ok", "mode": "agent-stub"})
        app2.run(host="0.0.0.0", port=PORT, debug=False)
        return

    env = {
        **os.environ,
        "PORT":         str(PORT),
        "MAIN_URL":     MAIN_SERVER_URL,
        "RAM_CACHE_MB": str(ram_cache_mb),
    }
    proc = subprocess.Popen([sys.executable, agent_path], env=env)
    proc.wait()


# ─────────────────────────────────────────────
#  BAŞLATMA
# ─────────────────────────────────────────────

optimize_all("main" if IS_MAIN else "agent")

if IS_MAIN:
    print(f"\n{'━'*56}")
    print(f"  ANA SUNUCU v10.0 — Panel :{PORT}")
    print(f"  NBD YOK → Resource Pool (HTTP) aktif")
    print(f"  Agent'lar bağlandıkça: RAM cache + disk store + proxy devreye girer")
    print(f"{'━'*56}\n")
    panel_proc = start_panel()
    threading.Thread(target=auto_start_sequence, daemon=True).start()
    panel_proc.wait()
else:
    run_agent_mode()
