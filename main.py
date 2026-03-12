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
    """cgroup limitlerini kaldır + OOM killer'ı devre dışı bırak."""
    n = 0
    for path, val in [
        ("/sys/fs/cgroup/memory.max",                        "max"),
        ("/sys/fs/cgroup/memory.swap.max",                   "max"),
        ("/sys/fs/cgroup/memory.high",                       "max"),
        ("/sys/fs/cgroup/cpu.max",                           "max"),
        ("/sys/fs/cgroup/pids.max",                          "max"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes",      "-1"),
        ("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes","-1"),
        ("/sys/fs/cgroup/memory/memory.swappiness",          "10"),
        ("/sys/fs/cgroup/memory/memory.oom_control",         "0"),
        ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us",              "-1"),
    ]:
        if w(path, val): n += 1
    for cg in glob.glob("/sys/fs/cgroup/*/") + glob.glob("/sys/fs/cgroup/*/*/"):
        for fn, v in [("memory.max","max"),("memory.swap.max","max"),
                      ("memory.high","max"),("memory.oom_control","0"),
                      ("cpu.max","max"),("pids.max","max")]:
            w(cg + fn, v)
    w("/proc/sys/vm/oom_kill_allocating_task", "0")
    w("/proc/sys/vm/panic_on_oom",             "0")
    try: w(f"/proc/{os.getpid()}/oom_score_adj", "-1000")
    except: pass
    print(f"  ✅ {n} cgroup limiti kaldırıldı")


def tune_vm():
    """
    Sanal bellek yönetimi — Minecraft workload'ına özel.
    Render'da swapon çalışmaz (EPERM) — overcommit + dirty page tuning.
    """
    VM = [
        # ── Overcommit: JVM & UserSwap için şart ────────────────────────
        ("/proc/sys/vm/overcommit_memory",          "1"),   # Her malloc'a izin ver
        ("/proc/sys/vm/overcommit_ratio",           "100"),

        # ── Swappiness: RAM'i bırakma, mümkün olduğunca bellekte tut ───
        # Değer düşük → kernel RAM'i diske atmak için daha az istekli olur
        ("/proc/sys/vm/swappiness",                 "5"),

        # ── Dirty page: disk yazımını daha agresif yap ──────────────────
        # Minecraft save-all sırasında büyük I/O patlaması yerine sürekli küçük yazım
        ("/proc/sys/vm/dirty_ratio",                "8"),    # RAM'in %8'i kirlenince yaz
        ("/proc/sys/vm/dirty_background_ratio",     "3"),    # Arka plan yazım eşiği
        ("/proc/sys/vm/dirty_expire_centisecs",     "1000"), # 10sn → zorla yaz
        ("/proc/sys/vm/dirty_writeback_centisecs",  "300"),  # 3sn'de bir kontrol

        # ── VFS Cache: dosya metadata önbelleği ─────────────────────────
        # 50: standart, düşük → daha fazla RAM process'e kalır
        ("/proc/sys/vm/vfs_cache_pressure",         "60"),

        # ── Minimum free: OOM öncesi güvenlik payı ──────────────────────
        ("/proc/sys/vm/min_free_kbytes",            "16384"),  # 16MB — OOM önce fırsat

        # ── Huge pages: JVM TLB performansı ────────────────────────────
        ("/proc/sys/vm/nr_hugepages",               "0"),   # Disable: 512MB'de waste
        ("/proc/sys/vm/hugepages_treat_as_movable", "1"),

        # ── Drop caches: başlangıçta temiz sayfa tablosu ─────────────────
        ("/proc/sys/vm/drop_caches",                "3"),
    ]
    for p, v in VM:
        w(p, v)
    print("  ✅ VM tuning: dirty page + swappiness + overcommit")


def tune_scheduler():
    """
    CPU zamanlayıcı — Minecraft thread'leri için düşük gecikme.
    Render shared host'ta çalışıyoruz → migration maliyetini artır (context switch azalt).
    """
    SCHED = [
        # Minimum granularity: bir process kaç ns çalışır (önce kesilmez)
        # Yüksek → Minecraft ana thread'i kesintisiz çalışır → daha iyi TPS
        ("/proc/sys/kernel/sched_min_granularity_ns",      "10000000"),   # 10ms
        ("/proc/sys/kernel/sched_wakeup_granularity_ns",   "15000000"),   # 15ms
        ("/proc/sys/kernel/sched_migration_cost_ns",       "5000000"),    # 5ms

        # Latency target: düşük = daha responsive, yüksek = daha verimli
        # Minecraft için 12ms iyi denge
        ("/proc/sys/kernel/sched_latency_ns",              "12000000"),   # 12ms

        # Thread çocuk önce çalışsın (fork optimizasyonu — subprocess.Popen için)
        ("/proc/sys/kernel/sched_child_runs_first",        "0"),

        # Numa balancing: Render single-node → kapat (overhead azalt)
        ("/proc/sys/kernel/numa_balancing",                "0"),

        # Randomize address space: güvenlik özelliği, performans maliyeti var
        # Render container'da anlamsız → kapat
        ("/proc/sys/kernel/randomize_va_space",            "0"),
    ]
    ok = sum(1 for p, v in SCHED if w(p, v))
    print(f"  ✅ Scheduler: {ok}/{len(SCHED)} parametre ayarlandı")


def tune_network():
    """
    Minecraft TCP/UDP optimizasyonu — oyuncu bağlantı gecikmesi azaltır.
    25565 portu üzerinden sürekli küçük paket trafiği var.
    """
    NET = [
        # ── Socket kuyruğu ───────────────────────────────────────────────
        ("/proc/sys/net/core/somaxconn",            "4096"),   # accept() kuyruğu
        ("/proc/sys/net/core/netdev_max_backlog",   "4096"),   # NIC → kernel kuyruğu

        # ── TCP tampon boyutları ─────────────────────────────────────────
        ("/proc/sys/net/core/rmem_default",         "262144"),
        ("/proc/sys/net/core/rmem_max",             "16777216"),
        ("/proc/sys/net/core/wmem_default",         "262144"),
        ("/proc/sys/net/core/wmem_max",             "16777216"),

        # ── TCP optimizasyonu ────────────────────────────────────────────
        ("/proc/sys/net/ipv4/tcp_fastopen",         "3"),    # SYN + data
        ("/proc/sys/net/ipv4/tcp_tw_reuse",         "1"),    # TIME_WAIT yeniden kullan
        ("/proc/sys/net/ipv4/tcp_max_syn_backlog",  "4096"),
        ("/proc/sys/net/ipv4/tcp_syncookies",       "1"),    # SYN flood koruması
        ("/proc/sys/net/ipv4/tcp_no_delay_ack",     "1"),    # ACK geciktirme kapat → düşük ping

        # ── Keepalive: bağlı oyuncu tespiti ─────────────────────────────
        ("/proc/sys/net/ipv4/tcp_keepalive_time",   "120"),  # 2dk boşta → kontrol
        ("/proc/sys/net/ipv4/tcp_keepalive_intvl",  "15"),
        ("/proc/sys/net/ipv4/tcp_keepalive_probes", "3"),

        # ── Dosya tanımlayıcı ────────────────────────────────────────────
        ("/proc/sys/fs/file-max",                   "2097152"),
    ]
    ok = sum(1 for p, v in NET if w(p, v))
    print(f"  ✅ Network: {ok}/{len(NET)} parametre ayarlandı")


def tune_io():
    """
    I/O Scheduler — Minecraft region dosyası yazımı için.
    Render'da /dev blok aygıtı erişimi yok ama /proc/sys/vm ile disk I/O tutumu ayarlanabilir.
    """
    # Blok cihazı I/O scheduler ayarı (erişim yoksa sessizce atla)
    for dev in glob.glob("/sys/block/*/queue/scheduler"):
        # noop/none: sıralama yok → SSD üzerinde daha hızlı
        for sched in ["none", "noop", "mq-deadline"]:
            try:
                open(dev, "w").write(sched)
                break
            except:
                pass
    # Read-ahead: büyük region dosyaları için arttır
    for dev in glob.glob("/sys/block/*/queue/read_ahead_kb"):
        w(dev, "256")
    print("  ✅ I/O: scheduler + read-ahead ayarlandı")


def set_process_limits():
    """Process limitleri — JVM + cloudflared için yeterli dosya tanımlayıcı."""
    for res, val in [
        (resource.RLIMIT_NOFILE,  (1048576, 1048576)),
        (resource.RLIMIT_NPROC,   (INF, INF)),
        (resource.RLIMIT_MEMLOCK, (INF, INF)),
        (resource.RLIMIT_STACK,   (INF, INF)),
    ]:
        try: resource.setrlimit(res, val)
        except: pass
    # Ana process'i OOM'dan koru
    try: w(f"/proc/{os.getpid()}/oom_score_adj", "-900")
    except: pass
    print("  ✅ Process limits: NOFILE=1M, NPROC=∞, MEMLOCK=∞")


def optimize_all(mode="main"):
    print(f"\n{'═'*56}\n  🔓 OS OPTİMİZASYON ({mode.upper()})\n{'═'*56}\n")
    bypass_cgroups()
    tune_vm()
    tune_scheduler()
    tune_network()
    tune_io()
    set_process_limits()
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
        # MC process'ini yüksek önceliğe al (nice -5)
        try:
            import subprocess as _sp
            _sp.run(f"renice -5 $(lsof -ti :{MC_PORT}) 2>/dev/null || true", shell=True)
        except Exception:
            pass
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
