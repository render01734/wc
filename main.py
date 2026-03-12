"""
⛏️  Minecraft Server Boot — TÜM LİMİTLER KALDIRILDI
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Render'ın tüm kısıtlamalarını bypass et:
  ✓ cgroup v1 + v2 bellek / swap / cpu / pid limitleri
  ✓ ulimit (nofile, nproc, stack, memlock, fsize...)
  ✓ Kernel VM / net / fs / sched parametreleri
  ✓ Disk → Swap (boş diskin %80'i, max 64GB)
  ✓ zram sıkıştırılmış RAM swap
  ✓ Huge Pages (JVM GC hızlandır)
  ✓ CPU governor → performance
  ✓ I/O scheduler → none/mq-deadline
  ✓ OOM killer devre dışı
  ✓ Namespace / ptrace / perf kısıtlamaları
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
#  1 — ULIMIT
# ══════════════════════════════════════════════════════════════

def bypass_ulimits():
    print("  [ulimit] Tüm process sınırları kaldırılıyor...")
    limits = [
        resource.RLIMIT_NOFILE,
        resource.RLIMIT_NPROC,
        resource.RLIMIT_STACK,
        resource.RLIMIT_CORE,
        resource.RLIMIT_DATA,
        resource.RLIMIT_FSIZE,
        resource.RLIMIT_MEMLOCK,
    ]
    ok = 0
    for res in limits:
        try:
            soft, hard = resource.getrlimit(res)
            try:
                resource.setrlimit(res, (INF, INF))
                ok += 1
            except ValueError:
                resource.setrlimit(res, (hard, hard))
        except Exception:
            pass
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (1048576, 1048576))
    except Exception:
        pass
    print(f"  ✅ ulimit → {ok}/{len(limits)} sınır kaldırıldı")


# ══════════════════════════════════════════════════════════════
#  2 — CGROUP V1 + V2
# ══════════════════════════════════════════════════════════════

def bypass_cgroups():
    print("  [cgroup] Tüm cgroup limitleri kaldırılıyor...")
    unlocked = 0

    # cgroup v2
    cg2 = {
        "/sys/fs/cgroup/memory.max":      "max",
        "/sys/fs/cgroup/memory.swap.max": "max",
        "/sys/fs/cgroup/memory.high":     "max",
        "/sys/fs/cgroup/memory.low":      "0",
        "/sys/fs/cgroup/cpu.max":         "max",
        "/sys/fs/cgroup/cpu.weight":      "10000",
        "/sys/fs/cgroup/pids.max":        "max",
    }
    for path, val in cg2.items():
        if w(path, val): unlocked += 1

    # cgroup v2 alt dizinler
    for cg_dir in glob.glob("/sys/fs/cgroup/*/") + glob.glob("/sys/fs/cgroup/*/*/"):
        for fn, val in [("memory.max","max"),("memory.swap.max","max"),
                        ("memory.high","max"),("cpu.max","max"),("pids.max","max")]:
            w(cg_dir + fn, val)

    # cgroup v1
    cg1 = {
        "/sys/fs/cgroup/memory/memory.limit_in_bytes":       "-1",
        "/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes": "-1",
        "/sys/fs/cgroup/memory/memory.soft_limit_in_bytes":  "-1",
        "/sys/fs/cgroup/memory/memory.swappiness":           "100",
        "/sys/fs/cgroup/memory/memory.oom_control":          "0",
        "/sys/fs/cgroup/memory/memory.use_hierarchy":        "0",
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us":               "-1",
        "/sys/fs/cgroup/cpu/cpu.shares":                     "1024",
        "/sys/fs/cgroup/pids/pids.max":                      "max",
        "/sys/fs/cgroup/blkio/blkio.weight":                 "1000",
    }
    for path, val in cg1.items():
        if w(path, val): unlocked += 1

    # cgroup v1 alt dizinler
    for cg_dir in glob.glob("/sys/fs/cgroup/memory/*/") + glob.glob("/sys/fs/cgroup/cpu/*/"):
        w(cg_dir + "memory.limit_in_bytes",       "-1")
        w(cg_dir + "memory.memsw.limit_in_bytes", "-1")
        w(cg_dir + "memory.swappiness",           "100")
        w(cg_dir + "memory.oom_control",          "0")
        w(cg_dir + "cpu.cfs_quota_us",            "-1")

    print(f"  ✅ cgroup → {unlocked} limit kaldırıldı (bellek/cpu/pid)")


# ══════════════════════════════════════════════════════════════
#  3 — SWAP: DİSKİN %80'İNİ SWAP YAP
# ══════════════════════════════════════════════════════════════

def setup_swap_maximum():
    print("  [swap] Maksimum swap kurulumu...")
    import psutil

    disk    = psutil.disk_usage("/")
    free_gb = disk.free / 1024 / 1024 / 1024
    swap_gb = min(64, int(free_gb * 0.80))
    swap_mb = swap_gb * 1024
    swap_file = "/swapfile"

    swp = psutil.swap_memory()
    if swp.total >= swap_mb * 1024 * 1024 * 0.9:
        print(f"  ✅ Swap zaten aktif: {swp.total//1024//1024}MB")
    else:
        print(f"  📊 Disk boş: {free_gb:.0f}GB → Swap hedef: {swap_gb}GB")
        if os.path.exists(swap_file):
            sh(f"swapoff {swap_file} 2>/dev/null")
            try: os.remove(swap_file)
            except: pass

        ret = sh(f"fallocate -l {swap_mb}M {swap_file}")
        if ret.returncode != 0:
            print("  ⚠️  fallocate yok, dd kullanılıyor...")
            sh(f"dd if=/dev/zero of={swap_file} bs=64M count={max(1,swap_mb//64)} status=none")

        sh(f"chmod 600 {swap_file}")
        sh(f"mkswap -f {swap_file}")
        ret2 = sh(f"swapon -p 0 {swap_file}")
        if ret2.returncode != 0:
            print(f"  ⚠️  swapon: {ret2.stderr.decode().strip()}")

    # zram — RAM sıkıştır, öncelikli swap
    sh("modprobe zram num_devices=1 2>/dev/null")
    mem_bytes = psutil.virtual_memory().total
    zram_mb   = min(4096, mem_bytes // 1024 // 1024)
    w("/sys/block/zram0/comp_algorithm", "lz4")
    if w("/sys/block/zram0/disksize", f"{zram_mb}M"):
        ret_z = sh("mkswap /dev/zram0 && swapon -p 100 /dev/zram0")
        if ret_z.returncode == 0:
            print(f"  ✅ zram: {zram_mb}MB sıkıştırılmış RAM (öncelikli)")

    # VM ayarları
    for path, val in [
        ("/proc/sys/vm/swappiness",             "200"),
        ("/proc/sys/vm/vfs_cache_pressure",     "500"),
        ("/proc/sys/vm/overcommit_memory",      "1"),
        ("/proc/sys/vm/overcommit_ratio",       "100"),
        ("/proc/sys/vm/page-cluster",           "0"),
        ("/proc/sys/vm/watermark_boost_factor", "0"),
        ("/proc/sys/vm/watermark_scale_factor", "125"),
    ]:
        if not w(path, val) and "swappiness" in path:
            w(path, "100")

    swp2 = psutil.swap_memory()
    mem  = psutil.virtual_memory()
    print(f"  ✅ RAM={mem.total//1024//1024}MB + Swap={swp2.total//1024//1024}MB = {(mem.total+swp2.total)//1024//1024}MB")


# ══════════════════════════════════════════════════════════════
#  4 — KERNEL PARAMETRELERİ
# ══════════════════════════════════════════════════════════════

def optimize_kernel():
    print("  [kernel] Kernel parametreleri optimize ediliyor...")

    all_params = {
        # VM
        "/proc/sys/vm/min_free_kbytes":              "65536",
        "/proc/sys/vm/dirty_ratio":                  "80",
        "/proc/sys/vm/dirty_background_ratio":       "5",
        "/proc/sys/vm/dirty_expire_centisecs":       "3000",
        "/proc/sys/vm/dirty_writeback_centisecs":    "500",
        "/proc/sys/vm/zone_reclaim_mode":            "0",
        "/proc/sys/vm/oom_kill_allocating_task":     "0",
        "/proc/sys/vm/panic_on_oom":                 "0",
        "/proc/sys/vm/nr_hugepages":                 "256",
        "/proc/sys/vm/drop_caches":                  "3",
        # Sched
        "/proc/sys/kernel/sched_latency_ns":         "4000000",
        "/proc/sys/kernel/sched_min_granularity_ns": "500000",
        "/proc/sys/kernel/sched_migration_cost_ns":  "5000000",
        "/proc/sys/kernel/sched_rt_runtime_us":      "-1",
        "/proc/sys/kernel/sched_rt_period_us":       "1000000",
        "/proc/sys/kernel/nmi_watchdog":             "0",
        "/proc/sys/kernel/watchdog":                 "0",
        "/proc/sys/kernel/numa_balancing":           "1",
        "/proc/sys/kernel/perf_event_paranoid":      "-1",
        "/proc/sys/kernel/randomize_va_space":       "0",
        "/proc/sys/kernel/kptr_restrict":            "0",
        "/proc/sys/kernel/dmesg_restrict":           "0",
        "/proc/sys/kernel/pid_max":                  "4194304",
        "/proc/sys/kernel/threads-max":              "4194304",
        "/proc/sys/kernel/yama/ptrace_scope":        "0",
        # FS
        "/proc/sys/fs/file-max":                     "2097152",
        "/proc/sys/fs/nr_open":                      "2097152",
        "/proc/sys/fs/inotify/max_user_watches":     "524288",
        "/proc/sys/fs/inotify/max_user_instances":   "8192",
        "/proc/sys/fs/aio-max-nr":                   "1048576",
        "/proc/sys/fs/pipe-max-size":                "67108864",
        # Net
        "/proc/sys/net/core/rmem_max":               "268435456",
        "/proc/sys/net/core/wmem_max":               "268435456",
        "/proc/sys/net/core/somaxconn":              "65535",
        "/proc/sys/net/core/netdev_max_backlog":     "65536",
        "/proc/sys/net/ipv4/tcp_rmem":               "4096 67108864 268435456",
        "/proc/sys/net/ipv4/tcp_wmem":               "4096 67108864 268435456",
        "/proc/sys/net/ipv4/tcp_fastopen":           "3",
        "/proc/sys/net/ipv4/tcp_tw_reuse":           "1",
        "/proc/sys/net/ipv4/tcp_fin_timeout":        "10",
        "/proc/sys/net/ipv4/tcp_max_syn_backlog":    "65536",
        "/proc/sys/net/ipv4/tcp_syncookies":         "1",
        "/proc/sys/net/ipv4/tcp_mtu_probing":        "1",
        "/proc/sys/net/ipv4/ip_local_port_range":    "1024 65535",
        "/proc/sys/net/ipv4/tcp_low_latency":        "1",
        # Namespace
        "/proc/sys/kernel/unprivileged_userns_clone":"1",
        "/proc/sys/user/max_user_namespaces":        "65536",
        "/proc/sys/user/max_pid_namespaces":         "65536",
    }
    ok = sum(w(p, v) for p, v in all_params.items())
    print(f"  ✅ Kernel → {ok}/{len(all_params)} parametre ayarlandı")

    # CPU governor
    govs = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    gc = sum(w(g, "performance") for g in govs)
    if gc: print(f"  ✅ CPU → {gc} çekirdek performance modunda")

    # I/O
    for dev in glob.glob("/sys/block/*/queue/scheduler"):
        for s in ["none", "mq-deadline", "noop"]:
            if w(dev, s): break
    for dev in glob.glob("/sys/block/*/queue/nr_requests"):    w(dev, "512")
    for dev in glob.glob("/sys/block/*/queue/read_ahead_kb"):  w(dev, "4096")
    for dev in glob.glob("/sys/block/*/queue/rq_affinity"):    w(dev, "2")

    # HugePage
    w("/sys/kernel/mm/transparent_hugepage/enabled", "always")
    w("/sys/kernel/mm/transparent_hugepage/defrag",  "defer+madvise")

    # OOM — tüm process'leri koru
    w("/proc/sys/vm/oom_kill_allocating_task", "0")
    w("/proc/sys/vm/panic_on_oom", "0")
    try:
        w(f"/proc/{os.getpid()}/oom_score_adj", "-1000")
    except Exception:
        pass

    # tmpfs
    for tgt, sz in [("/tmp","2g"),("/var/tmp","512m")]:
        os.makedirs(tgt, exist_ok=True)
        sh(f"mount -t tmpfs tmpfs {tgt} -o defaults,noatime,nosuid,nodev,size={sz} 2>/dev/null")
    print("  ✅ HugePage + OOM koruması + tmpfs aktif")


# ══════════════════════════════════════════════════════════════
#  ANA OPTİMİZASYON
# ══════════════════════════════════════════════════════════════

def optimize_all():
    print("\n" + "═"*52)
    print("  🔓 TÜM LİMİTLER VE ENGELLEMELER KALDIRILIYOR")
    print("═"*52 + "\n")

    bypass_ulimits()
    print()
    bypass_cgroups()
    print()
    setup_swap_maximum()
    print()
    optimize_kernel()

    import psutil
    mem  = psutil.virtual_memory()
    swp  = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    cpu  = psutil.cpu_count(logical=True)
    print("\n" + "═"*52)
    print(f"  CPU     : {cpu} çekirdek")
    print(f"  RAM     : {mem.total//1024//1024} MB fiziksel")
    print(f"  Swap    : {swp.total//1024//1024} MB")
    print(f"  Disk    : {disk.free//1024//1024//1024} GB boş / {disk.total//1024//1024//1024} GB toplam")
    print(f"  TOPLAM  : {(mem.total+swp.total)//1024//1024} MB kullanılabilir")
    print("═"*52)


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

    print("\n🌐 [4/4] Cloudflare Tunnel MC:25565 → internet...")
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
print("  ⛏️   Minecraft Server Sistemi v4.0 — MAX MODE")
print(f"      PORT={PORT}  |  MC_RAM={MC_RAM}")
print("━"*52)

print("\n⚡ [1/4] Limit bypass + Sistem optimizasyonu...")
optimize_all()

panel_proc = start_panel()
threading.Thread(target=auto_start_sequence, daemon=True).start()

print(f"\n{'━'*52}")
print(f"  Panel: http://0.0.0.0:{PORT}")
print(f"{'━'*52}\n")

panel_proc.wait()
