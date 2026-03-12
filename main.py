"""
⛏️  Minecraft Server Boot — RENDER BYPASS v5.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Render kısıtlamaları:
  - 512MB fiziksel RAM cgroup limiti  → cgroup memsw sınırı kaldır
  - 18GB disk limiti                  → swap MAX 8GB (güvenli)
  - Swap kurul → SONRA JVM başlat     → OOM yok
"""

import os, sys, subprocess, time, socket, resource, threading, re, glob

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
#  AŞAMA 1 — CGROUP BYPASS (privileged=true ile çalışır)
# ══════════════════════════════════════════════════════════════

def bypass_cgroups():
    print("  [cgroup] Limitler kaldırılıyor...")
    n = 0

    # cgroup v2
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

    # cgroup v1
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

    # OOM koruması — bu process öldürülmesin
    w("/proc/sys/vm/oom_kill_allocating_task", "0")
    w("/proc/sys/vm/panic_on_oom",             "0")
    try:
        w(f"/proc/{os.getpid()}/oom_score_adj", "-1000")
    except Exception:
        pass

    print(f"  ✅ cgroup → {n} limit kaldırıldı")


# ══════════════════════════════════════════════════════════════
#  AŞAMA 2 — SWAP KURULUMU (MAX 8GB — 18GB disk limitine uygun)
# ══════════════════════════════════════════════════════════════

def setup_swap():
    print("  [swap] Disk → Swap dönüşümü...")
    import psutil

    # Render disk limiti: 18GB toplam
    # Güvenli swap hedefi: 8GB (server.jar+world+log için 10GB bırak)
    disk     = psutil.disk_usage("/")
    free_gb  = disk.free / 1024 / 1024 / 1024
    # Max 8GB, ama diskin %50'sini de geçme
    swap_gb  = min(8, int(free_gb * 0.50))
    swap_gb  = max(2, swap_gb)
    swap_mb  = swap_gb * 1024
    swap_file = "/swapfile"

    swp = psutil.swap_memory()
    if swp.total >= swap_mb * 1024 * 1024 * 0.8:
        print(f"  ✅ Swap zaten aktif: {swp.total//1024//1024}MB")
    else:
        print(f"  📊 Disk boş: {free_gb:.1f}GB → Swap: {swap_gb}GB oluşturuluyor...")

        # Varsa kaldır
        if os.path.exists(swap_file):
            sh(f"swapoff {swap_file} 2>/dev/null")
            try: os.remove(swap_file)
            except: pass

        # Oluştur
        ret = sh(f"fallocate -l {swap_mb}M {swap_file}")
        if ret.returncode != 0:
            sh(f"dd if=/dev/zero of={swap_file} bs=64M count={swap_mb//64} status=none")

        sh(f"chmod 600 {swap_file}")
        sh(f"mkswap -f {swap_file}")
        ret2 = sh(f"swapon -p 0 {swap_file}")

        if ret2.returncode == 0:
            print(f"  ✅ Swap dosyası aktif: {swap_mb}MB")
        else:
            print(f"  ⚠️  swapon: {ret2.stderr.decode().strip()}")

    # zram — RAM'i sıkıştır (yüksek öncelik)
    sh("modprobe zram num_devices=1 2>/dev/null")
    mem_mb  = psutil.virtual_memory().total // 1024 // 1024
    zram_mb = min(2048, mem_mb // 2)
    w("/sys/block/zram0/comp_algorithm", "lz4")
    if w("/sys/block/zram0/disksize", f"{zram_mb}M"):
        if sh("mkswap /dev/zram0 && swapon -p 100 /dev/zram0").returncode == 0:
            print(f"  ✅ zram: {zram_mb}MB sıkıştırılmış RAM (öncelikli swap)")

    # Kernel swap davranışı
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
    print(f"  🎯 RAM={mem.total//1024//1024}MB + Swap={swp2.total//1024//1024}MB = {total_mb}MB kullanılabilir")
    return total_mb


# ══════════════════════════════════════════════════════════════
#  AŞAMA 3 — KERNEL OPTİMİZASYONU
# ══════════════════════════════════════════════════════════════

def optimize_kernel():
    print("  [kernel] Parametreler ayarlanıyor...")

    params = {
        "/proc/sys/kernel/pid_max":                  "4194304",
        "/proc/sys/kernel/threads-max":              "4194304",
        "/proc/sys/kernel/sched_rt_runtime_us":      "-1",
        "/proc/sys/kernel/sched_latency_ns":         "4000000",
        "/proc/sys/kernel/sched_min_granularity_ns": "500000",
        "/proc/sys/kernel/perf_event_paranoid":      "-1",
        "/proc/sys/kernel/kptr_restrict":            "0",
        "/proc/sys/kernel/dmesg_restrict":           "0",
        "/proc/sys/kernel/yama/ptrace_scope":        "0",
        "/proc/sys/kernel/nmi_watchdog":             "0",
        "/proc/sys/kernel/randomize_va_space":       "0",
        "/proc/sys/fs/file-max":                     "2097152",
        "/proc/sys/fs/nr_open":                      "2097152",
        "/proc/sys/fs/inotify/max_user_watches":     "524288",
        "/proc/sys/net/core/rmem_max":               "134217728",
        "/proc/sys/net/core/wmem_max":               "134217728",
        "/proc/sys/net/core/somaxconn":              "65535",
        "/proc/sys/net/ipv4/tcp_tw_reuse":           "1",
        "/proc/sys/net/ipv4/tcp_fin_timeout":        "10",
        "/proc/sys/net/ipv4/tcp_fastopen":           "3",
        "/proc/sys/net/ipv4/tcp_max_syn_backlog":    "65536",
        "/proc/sys/net/ipv4/ip_local_port_range":    "1024 65535",
        "/proc/sys/user/max_user_namespaces":        "65536",
    }
    ok = sum(w(p, v) for p, v in params.items())

    # ulimits
    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (INF, INF)),
        (resource.RLIMIT_STACK,   (INF, INF)),
        (resource.RLIMIT_CORE,    (INF, INF)),
        (resource.RLIMIT_MEMLOCK, (INF, INF)),
    ]:
        try: resource.setrlimit(res, val)
        except Exception: pass

    # CPU governor
    govs = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    gc = sum(w(g, "performance") for g in govs)

    # I/O scheduler
    for dev in glob.glob("/sys/block/*/queue/scheduler"):
        for s in ["none", "mq-deadline", "noop"]:
            if w(dev, s): break
    for dev in glob.glob("/sys/block/*/queue/nr_requests"):   w(dev, "256")
    for dev in glob.glob("/sys/block/*/queue/read_ahead_kb"): w(dev, "1024")

    # HugePage
    w("/sys/kernel/mm/transparent_hugepage/enabled", "madvise")
    w("/sys/kernel/mm/transparent_hugepage/defrag",  "defer+madvise")

    print(f"  ✅ {ok}/{len(params)} parametre | CPU gov: {gc} çekirdek | HugePage aktif")


# ══════════════════════════════════════════════════════════════
#  ANA OPTİMİZASYON — SIRALAMASI ÖNEMLİ
# ══════════════════════════════════════════════════════════════

def optimize_all():
    print("\n" + "═"*52)
    print("  🔓 RENDER BYPASS — TÜM LİMİTLER KALDIRILIYOR")
    print("═"*52 + "\n")

    # 1. Önce cgroup bypass (RAM limiti kaldır)
    bypass_cgroups()
    print()

    # 2. Swap kur (cgroup kaldırıldıktan SONRA — yoksa swapon engellenir)
    total_mb = setup_swap()
    print()

    # 3. Kernel optimize
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
#  PANEL + MC + TUNNEL
# ══════════════════════════════════════════════════════════════

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
        print("  ⚠️  MC portu zaman aşımı — tunnel yine de açılıyor")

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


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

print("\n" + "━"*52)
print("  ⛏️   Minecraft Server — RENDER BYPASS v5.0")
print(f"      PORT={PORT}  |  MC_RAM={MC_RAM}")
print("━"*52)

print("\n⚡ [1/4] Limit bypass + Optimizasyon...")
optimize_all()

panel_proc = start_panel()
threading.Thread(target=auto_start_sequence, daemon=True).start()

print(f"\n{'━'*52}")
print(f"  Panel: http://0.0.0.0:{PORT}")
print(f"{'━'*52}\n")

panel_proc.wait()
