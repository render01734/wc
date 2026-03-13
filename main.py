"""
⛏️  Minecraft Server Boot — v14.0 (Cuberite)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v14.0 (Cuberite):
  • ANA sunucu: Flask + Cuberite C++ binary
  • AGENT modu: agent.py'yi başlat, ana sunucuya kayıt ol
  • Swap: zram → disk dosyası (hem ANA hem AGENT için)
  • OOM: overcommit=1, oom_score_adj=-900 (Cuberite korunsun)
  • Cuberite ~50MB RAM — JVM/userswap gerektirmez
"""

import os, sys, subprocess, time, socket, resource, threading, re, json
import glob, shutil
import psutil
import urllib.request as _ur

# ── Sabitler ─────────────────────────────────────────────────────────────────
RENDER_DISK_LIMIT_GB  = float(os.environ.get("RENDER_DISK_LIMIT_GB", "18.0"))
RENDER_RAM_LIMIT_MB   = 512
MAIN_SERVER_URL       = "https://wc-tsgd.onrender.com"
MY_URL  = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
IS_MAIN = (MY_URL == MAIN_SERVER_URL or MY_URL == ""
           or os.environ.get("FORCE_MAIN", "") == "1")
PORT    = int(os.environ.get("PORT", "5000"))
MC_PORT = 25565
INF     = resource.RLIM_INFINITY


# ── cgroup RAM okuma ──────────────────────────────────────────────────────────

def _cgroup_ram_mb() -> int:
    for path in ["/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
        try:
            val = open(path).read().strip()
            if val in ("max", "-1", "9223372036854771712"):
                continue
            mb = int(val) // 1024 // 1024
            if 64 < mb < 65536:
                return min(mb, RENDER_RAM_LIMIT_MB)
        except Exception:
            pass
    return RENDER_RAM_LIMIT_MB


CONTAINER_RAM_MB = _cgroup_ram_mb()

base_env = {
    **os.environ,
    "HOME": "/root", "USER": "root", "LOGNAME": "root",
    "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
    # Java gerektirmiyor — Cuberite C++ binary
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "PORT":             str(PORT),
    "CONTAINER_RAM_MB": str(CONTAINER_RAM_MB),
}

print("\n" + "━" * 56)
print("  ⛏️   Minecraft Server — v14.0 (Cuberite)")
print(f"      MOD    : {'🟢 ANA' if IS_MAIN else '🔵 AGENT'}")
print(f"      MY_URL : {MY_URL or '(boş → ANA)'}")
print(f"      RAM    : {CONTAINER_RAM_MB}MB")
print("━" * 56 + "\n")


# ── Yardımcılar ──────────────────────────────────────────────────────────────

def _w(path: str, val) -> bool:
    try:
        open(path, "w").write(str(val))
        return True
    except Exception:
        return False


def _sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True)


def _wait_port(port: int, timeout: int = 60) -> bool:
    for _ in range(timeout * 10):
        try:
            s = socket.create_connection(("127.0.0.1", port), 0.1)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _panel_log(msg: str):
    try:
        _ur.urlopen(_ur.Request(
            f"http://localhost:{PORT}/api/internal/status_msg",
            data=json.dumps({"msg": msg}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ), timeout=2)
    except Exception:
        pass


# ── Cgroup bypass ─────────────────────────────────────────────────────────────

def bypass_cgroups():
    n = 0
    for path, val in [
        ("/sys/fs/cgroup/memory.max",                      "max"),
        ("/sys/fs/cgroup/memory.swap.max",                 "max"),
        ("/sys/fs/cgroup/memory.high",                     "max"),
        ("/sys/fs/cgroup/memory.oom.group",                "0"),
        ("/sys/fs/cgroup/cpu.max",                         "max"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes",    "-1"),
        ("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes", "-1"),
        ("/sys/fs/cgroup/memory/memory.oom_control",       "0"),
    ]:
        if _w(path, val):
            n += 1

    for cg in glob.glob("/sys/fs/cgroup/*/") + glob.glob("/sys/fs/cgroup/*/*/"):
        for fn, v in [("memory.max","max"),("memory.swap.max","max"),
                      ("memory.high","max"),("memory.oom.group","0"),
                      ("cpu.max","max")]:
            _w(cg + fn, v)

    _w("/proc/sys/vm/oom_kill_allocating_task", "0")
    _w("/proc/sys/vm/panic_on_oom",             "0")
    _w("/proc/sys/vm/overcommit_memory",        "1")
    _w("/proc/sys/vm/overcommit_ratio",         "100")
    try:
        _w(f"/proc/{os.getpid()}/oom_score_adj", "-999")
    except Exception:
        pass
    print(f"  ✅ cgroup bypass: {n} yaz + overcommit=1")


# ── Swap kurulum ──────────────────────────────────────────────────────────────

def setup_swap() -> int:
    total = 0

    # zram (RAM içi sıkıştırılmış swap — çok hızlı)
    _sh("modprobe zram num_devices=1 2>/dev/null")
    for algo in ["lz4", "zstd", "lzo"]:
        if _w("/sys/block/zram0/comp_algorithm", algo):
            break
    zram_mb = max(256, CONTAINER_RAM_MB * 3 // 4)
    if _w("/sys/block/zram0/disksize", f"{zram_mb}M"):
        r = _sh("mkswap /dev/zram0 2>/dev/null && swapon -p 200 /dev/zram0")
        if r.returncode == 0:
            total += zram_mb
            print(f"  ✅ zram: {zram_mb}MB (prio:200)")

    # Disk swap dosyası
    dk    = shutil.disk_usage("/")
    avail = dk.free / 1e9 - 2.0  # 2GB güvenlik payı
    sw_mb = int(min(6.0, max(0.0, avail)) * 1024)
    if sw_mb >= 512:
        sf = "/swapfile"
        _sh(f"swapoff {sf} 2>/dev/null")
        try: os.remove(sf)
        except Exception: pass
        r = _sh(f"fallocate -l {sw_mb}M {sf} 2>/dev/null")
        if r.returncode != 0 or not os.path.exists(sf):
            blk = 64
            cnt = max(1, sw_mb // blk)
            _sh(f"dd if=/dev/zero of={sf} bs={blk}M count={cnt} status=none 2>/dev/null")
        if os.path.exists(sf) and os.path.getsize(sf) > 0:
            _sh(f"chmod 600 {sf} && mkswap -f {sf} 2>/dev/null")
            r = _sh(f"swapon -p 100 {sf} 2>/dev/null")
            if r.returncode == 0:
                actual = os.path.getsize(sf) // 1024 // 1024
                total += actual
                print(f"  ✅ Disk swap: {actual}MB (prio:100)")

    # Kernel sanal bellek ayarları
    for p, v in [
        ("/proc/sys/vm/swappiness",             "200"),
        ("/proc/sys/vm/vfs_cache_pressure",     "500"),
        ("/proc/sys/vm/overcommit_memory",      "1"),
        ("/proc/sys/vm/overcommit_ratio",       "100"),
        ("/proc/sys/vm/page-cluster",           "0"),
        ("/proc/sys/vm/min_free_kbytes",        "16384"),
        ("/proc/sys/vm/dirty_ratio",            "80"),
        ("/proc/sys/vm/dirty_background_ratio", "50"),
    ]:
        _w(p, v)

    swp = psutil.swap_memory()
    print(f"  ✅ Toplam swap: {swp.total//1024//1024}MB")
    return swp.total // 1024 // 1024


# UserSwap YOK — Cuberite C++ mmap hook gerektirmiyor


# ── Kernel ayarları ───────────────────────────────────────────────────────────

def optimize_kernel():
    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (INF, INF)),
        (resource.RLIMIT_MEMLOCK, (INF, INF)),
    ]:
        try:
            resource.setrlimit(res, val)
        except Exception:
            pass


def optimize_all(mode: str = "main"):
    print(f"\n{'═'*56}\n  🔓 BYPASS ({mode.upper()})\n{'═'*56}\n")
    bypass_cgroups()
    sw = setup_swap()
    optimize_kernel()
    print(f"  ✅ Swap:{sw}MB  RAM:{CONTAINER_RAM_MB}MB\n{'═'*56}")
    return sw


# ── Panel başlatma ────────────────────────────────────────────────────────────

def start_panel():
    """
    mc_panel.py başlatır. Cuberite C++ ~50MB → Flask+MC birlikte rahat çalışır.
    MC_ONLY=1 env ile sadece Cuberite + minimal HTTP (Flask yok) da çalışır.
    """
    print(f"\n🚀 MC Panel v14.0 (Cuberite) başlatılıyor :{PORT}...")
    env = {**base_env}
    proc = subprocess.Popen([sys.executable, "/app/mc_panel.py"], env=env)
    if _wait_port(PORT, 30):
        print(f"  ✅ Panel hazır :{PORT}")
    else:
        print(f"  ⚠️  Port {PORT} timeout — devam ediliyor")
    return proc


# ── MC otomatik başlatma ──────────────────────────────────────────────────────

def auto_start():
    """
    Cuberite hazır olduktan sonra Cloudflare tüneli açar.
    """
    _panel_log("[Sistem] 🟢 v14.0 Cuberite başladı")
    if _wait_port(MC_PORT, 120):
        print("  ✅ Cuberite hazır!")
        _panel_log("[Sistem] ✅ Cuberite çalışıyor — tünel açılıyor")
        _start_mc_tunnel()
    else:
        print("  ⚠️  Cuberite port timeout (120s)")


def _start_mc_tunnel():
    log = "/tmp/cf_mc.log"
    subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"tcp://localhost:{MC_PORT}",
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
                    _ur.urlopen(_ur.Request(
                        f"http://localhost:{PORT}/api/internal/tunnel",
                        data=json.dumps({"url": url, "host": host}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ), timeout=3)
                except Exception:
                    pass
                return
        except Exception:
            pass
        time.sleep(0.5)


# ── Agent modu ────────────────────────────────────────────────────────────────

def run_agent():
    agent_path   = "/app/agent.py"
    ram_cache_mb = max(200, CONTAINER_RAM_MB - 130)

    print(f"\n{'═'*56}")
    print(f"  🔵 AGENT MODU v14.0 (Cuberite)")
    print(f"  Ana    : {MAIN_SERVER_URL}")
    print(f"  Cache  : {ram_cache_mb}MB")
    print(f"{'═'*56}\n")

    if not os.path.exists(agent_path):
        print("  ⚠️  /app/agent.py bulunamadı!")
        # Minimal sağlık sunucusu
        from flask import Flask, jsonify
        a = Flask(__name__)
        @a.route("/")
        @a.route("/health")
        def _h():
            return jsonify({"status": "ok", "mode": "stub"})
        a.run(host="0.0.0.0", port=PORT)
        return

    env = {
        **os.environ,
        "PORT":         str(PORT),
        "MAIN_URL":     MAIN_SERVER_URL,
        "RAM_CACHE_MB": str(ram_cache_mb),
        "DISK_LIMIT_GB": os.environ.get("DISK_LIMIT_GB", "10.0"),
    }
    proc = subprocess.Popen([sys.executable, agent_path], env=env)
    proc.wait()


# ══════════════════════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════════════════════

optimize_all("main" if IS_MAIN else "agent")

if IS_MAIN:
    print(f"\n{'━'*56}")
    print(f"  🟢 ANA SUNUCU v14.0 (Cuberite) — Flask + Cuberite :{PORT}")
    print(f"  Cuberite: ~50MB RAM  Flask: ~90MB  Toplam: ~140MB")
    print(f"  Agent'lar: RAM cache + disk store aktif")
    print(f"{'━'*56}\n")
    panel = start_panel()
    threading.Thread(target=auto_start, daemon=True).start()
    panel.wait()
else:
    run_agent()
