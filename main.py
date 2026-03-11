"""
⛏️  Minecraft Server Boot Sistemi
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Maksimum kernel/sistem optimizasyonu
2. Panel başlat (Flask+SocketIO, PORT)
3. Minecraft Server otomatik başlat
4. Cloudflare Tunnel (MC portu → internet)
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


def w(path, val):
    try:
        with open(path, "w") as f:
            f.write(str(val))
        return True
    except Exception:
        return False


def wait_port(port, timeout=60):
    for _ in range(timeout * 10):
        try:
            s = socket.create_connection(("127.0.0.1", int(port)), 0.1)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


# ══════════════════════════════════════════════════════════
#  AŞAMA 1 — MAKSİMUM SİSTEM OPTİMİZASYONU
# ══════════════════════════════════════════════════════════

def optimize():
    print("⚡ Maksimum sistem optimizasyonu başlıyor...\n")

    # ── ulimits: tüm sınırları kaldır ─────────────────────
    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (resource.RLIM_INFINITY, resource.RLIM_INFINITY)),
        (resource.RLIMIT_STACK,   (resource.RLIM_INFINITY, resource.RLIM_INFINITY)),
        (resource.RLIMIT_CORE,    (resource.RLIM_INFINITY, resource.RLIM_INFINITY)),
        (resource.RLIMIT_MEMLOCK, (resource.RLIM_INFINITY, resource.RLIM_INFINITY)),
    ]:
        try:
            resource.setrlimit(res, val)
        except Exception:
            pass
    print("  ✅ ulimits   → sınırsız (nofile=1M, nproc=∞, stack=∞)")

    # ── VM / Bellek ────────────────────────────────────────
    vm_params = {
        "/proc/sys/vm/swappiness":               "1",
        "/proc/sys/vm/vfs_cache_pressure":       "50",
        "/proc/sys/vm/overcommit_memory":        "1",
        "/proc/sys/vm/overcommit_ratio":         "100",
        "/proc/sys/vm/dirty_ratio":              "80",
        "/proc/sys/vm/dirty_background_ratio":   "10",
        "/proc/sys/vm/dirty_expire_centisecs":   "3000",
        "/proc/sys/vm/dirty_writeback_centisecs": "500",
        "/proc/sys/vm/min_free_kbytes":          "65536",
        "/proc/sys/vm/oom_kill_allocating_task": "0",
        "/proc/sys/vm/panic_on_oom":             "0",
        "/proc/sys/vm/zone_reclaim_mode":        "0",
        "/proc/sys/vm/page-cluster":             "3",
    }
    ok = sum(w(p, v) for p, v in vm_params.items())
    print(f"  ✅ VM/bellek → {ok}/{len(vm_params)} parametre (swappiness=1, overcommit=on)")

    # ── CPU Zamanlayıcı — düşük gecikme ───────────────────
    sched_params = {
        "/proc/sys/kernel/sched_latency_ns":           "4000000",
        "/proc/sys/kernel/sched_min_granularity_ns":   "500000",
        "/proc/sys/kernel/sched_wakeup_granularity_ns":"1000000",
        "/proc/sys/kernel/sched_migration_cost_ns":    "5000000",
        "/proc/sys/kernel/sched_rt_runtime_us":        "-1",
        "/proc/sys/kernel/sched_rt_period_us":         "1000000",
        "/proc/sys/kernel/nmi_watchdog":               "0",
        "/proc/sys/kernel/watchdog":                   "0",
        "/proc/sys/kernel/perf_event_paranoid":        "-1",
        "/proc/sys/kernel/kptr_restrict":              "0",
        "/proc/sys/kernel/numa_balancing":             "1",
        "/proc/sys/kernel/randomize_va_space":         "0",
    }
    ok = sum(w(p, v) for p, v in sched_params.items())
    print(f"  ✅ CPU sched → {ok}/{len(sched_params)} (RT sınırsız, low-latency)")

    # ── CPU governor: performance ──────────────────────────
    govs = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    gc = sum(w(g, "performance") for g in govs)
    if gc:
        print(f"  ✅ CPU gov   → {gc} çekirdek: performance modu")

    # ── Filesystem ────────────────────────────────────────
    fs_params = {
        "/proc/sys/fs/file-max":                "2097152",
        "/proc/sys/fs/nr_open":                 "2097152",
        "/proc/sys/fs/inotify/max_user_watches":"524288",
        "/proc/sys/fs/inotify/max_user_instances":"8192",
        "/proc/sys/fs/aio-max-nr":              "1048576",
    }
    ok = sum(w(p, v) for p, v in fs_params.items())
    print(f"  ✅ FS         → {ok}/{len(fs_params)} (file-max=2M)")

    # ── I/O Scheduler — SSD optimize ──────────────────────
    io_count = 0
    for dev in glob.glob("/sys/block/*/queue/scheduler"):
        for s in ["none", "mq-deadline", "noop"]:
            if w(dev, s):
                io_count += 1
                break
    for dev in glob.glob("/sys/block/*/queue/nr_requests"):
        w(dev, "256")
    for dev in glob.glob("/sys/block/*/queue/read_ahead_kb"):
        w(dev, "256")
    for dev in glob.glob("/sys/block/*/queue/rq_affinity"):
        w(dev, "2")
    if io_count:
        print(f"  ✅ I/O sched → {io_count} disk optimize (none/mq-deadline)")

    # ── Network TCP — Minecraft oyuncu paketleri ──────────
    net_params = {
        "/proc/sys/net/core/rmem_max":             "134217728",
        "/proc/sys/net/core/wmem_max":             "134217728",
        "/proc/sys/net/core/rmem_default":         "16777216",
        "/proc/sys/net/core/wmem_default":         "16777216",
        "/proc/sys/net/core/netdev_max_backlog":   "16384",
        "/proc/sys/net/core/somaxconn":            "65535",
        "/proc/sys/net/core/optmem_max":           "65536",
        "/proc/sys/net/ipv4/tcp_rmem":             "4096 16777216 134217728",
        "/proc/sys/net/ipv4/tcp_wmem":             "4096 65536 134217728",
        "/proc/sys/net/ipv4/tcp_mem":              "786432 1048576 26214400",
        "/proc/sys/net/ipv4/tcp_fastopen":         "3",
        "/proc/sys/net/ipv4/tcp_tw_reuse":         "1",
        "/proc/sys/net/ipv4/tcp_fin_timeout":      "10",
        "/proc/sys/net/ipv4/tcp_keepalive_time":   "300",
        "/proc/sys/net/ipv4/tcp_keepalive_probes": "5",
        "/proc/sys/net/ipv4/tcp_keepalive_intvl":  "15",
        "/proc/sys/net/ipv4/tcp_max_syn_backlog":  "8192",
        "/proc/sys/net/ipv4/tcp_syncookies":       "1",
        "/proc/sys/net/ipv4/tcp_no_delay_ack":     "1",
        "/proc/sys/net/ipv4/tcp_low_latency":      "1",
        "/proc/sys/net/ipv4/tcp_mtu_probing":      "1",
        "/proc/sys/net/ipv4/ip_local_port_range":  "1024 65535",
    }
    ok = sum(w(p, v) for p, v in net_params.items())
    print(f"  ✅ Network   → {ok}/{len(net_params)} (TCP 128MB buffer, low-latency)")

    # ── tmpfs: /tmp → RAM diski ────────────────────────────
    for tgt, sz in [("/tmp", "1g"), ("/var/tmp", "256m")]:
        os.makedirs(tgt, exist_ok=True)
        r = subprocess.run(
            ["mount", "-t", "tmpfs", "tmpfs", tgt, "-o",
             f"defaults,noatime,nosuid,nodev,size={sz}"],
            capture_output=True
        )
        if r.returncode == 0:
            print(f"  ✅ tmpfs     → {tgt} = {sz} RAM disk")

    # ── Huge Pages: JVM GC hızlandır ─────────────────────
    w("/proc/sys/vm/nr_hugepages", "256")
    w("/sys/kernel/mm/transparent_hugepage/enabled", "always")
    w("/sys/kernel/mm/transparent_hugepage/defrag",  "defer+madvise")
    w("/sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs", "1000")
    print("  ✅ HugePage  → aktif (JVM GC hızlandırma)")

    # ── Kernel cache temizle ───────────────────────────────
    w("/proc/sys/vm/drop_caches", "3")
    print("  ✅ Cache     → temizlendi\n")
    print("  🎯 Sistem maksimum performans modunda!")


# ══════════════════════════════════════════════════════════
#  AŞAMA 2 — PANEL + MC + TUNNEL
# ══════════════════════════════════════════════════════════

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
    """Panelden sonra MC'yi otomatik başlat ve tunnel aç"""
    time.sleep(2)

    # MC'yi başlat
    print("\n⛏️  [3/4] Minecraft Server otomatik başlatılıyor...")
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

    # MC portu hazır olana kadar bekle
    print("  ⏳ MC Server başlaması bekleniyor (max 3 dk)...")
    if wait_port(MC_PORT, 180):
        print("  ✅ MC Server hazır!")
    else:
        print("  ⚠️  MC portu zaman aşımı — tunnel yine de açılıyor")

    # Cloudflare Tunnel
    print("\n🌐 [4/4] Cloudflare Tunnel MC:25565 → internet...")
    log = "/tmp/cf_mc.log"
    subprocess.Popen([
        "cloudflared", "tunnel",
        "--url", f"tcp://localhost:{MC_PORT}",
        "--no-autoupdate", "--loglevel", "info",
    ], stdout=open(log, "w"), stderr=subprocess.STDOUT)

    tunnel_url = ""
    for _ in range(60):
        try:
            content = open(log).read()
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', content)
            if urls:
                tunnel_url = urls[0]
                host = tunnel_url.replace("https://", "")
                print(f"\n  ┌─────────────────────────────────────────┐")
                print(f"  │  ✅ MC Sunucu Adresi:                    │")
                print(f"  │  📌 {host:<39}│")
                print(f"  └─────────────────────────────────────────┘\n")
                # Paneli bilgilendir
                try:
                    data = json.dumps({"url": tunnel_url, "host": host}).encode()
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

    print("  ⚠️  Tunnel URL alınamadı — MC dahili çalışıyor (port 25565)")


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

print("\n" + "━" * 50)
print("  ⛏️   Minecraft Server Sistemi v2.0")
print(f"      PORT={PORT}  |  MC_RAM={MC_RAM}")
print("━" * 50)

print("\n⚡ [1/4] Sistem optimizasyonu...")
optimize()

panel_proc = start_panel()

threading.Thread(target=auto_start_sequence, daemon=True).start()

print(f"\n{'━'*50}")
print(f"  Panel: http://0.0.0.0:{PORT}")
print(f"  MC bağlantı adresi konsola yazılacak...")
print(f"{'━'*50}\n")

panel_proc.wait()
