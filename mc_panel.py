"""
⛏️  Minecraft Yönetim Paneli — Tam Sürüm
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Flask + Flask-SocketIO + Eventlet
"""

import os, sys, json, time, threading, subprocess, shutil, zipfile
import re, glob, requests
from collections import deque
from datetime import datetime
from pathlib import Path

import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, send_file, abort, Response
from flask_socketio import SocketIO, emit

# ── Ayarlar ───────────────────────────────────────────────────
MC_DIR    = Path("/minecraft")
MC_JAR    = MC_DIR / "server.jar"
MC_PORT   = 25565
PANEL_PORT= int(os.environ.get("PORT", "5000"))
MC_VERSION= "1.21.1"
MC_RAM    = os.environ.get("MC_RAM", "2G")

# ── Global durum ─────────────────────────────────────────────
mc_process   = None
console_buf  = deque(maxlen=3000)
players      = {}
tunnel_info  = {"url": "", "host": ""}
server_state = {
    "status":  "stopped",
    "tps":     20.0,
    "tps15":   20.0,
    "tps5":    20.0,
    "ram_mb":  0,
    "uptime":  0,
    "started": None,
    "version": "—",
    "max_players": 20,
    "online_players": 0,
}

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
        name = m.group(1)
        players[name] = {"op": False, "joined_ts": time.time()}
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list())
        socketio.emit("stats_update", server_state)
        return

    m = re.search(r"(\w+) lost connection|(\w+) left the game", line)
    if m:
        name = m.group(1) or m.group(2)
        players.pop(name, None)
        server_state["online_players"] = len(players)
        socketio.emit("players_update", _players_list())
        socketio.emit("stats_update", server_state)
        return

    m = re.search(r"TPS from last 1m, 5m, 15m: ([\d.]+),\s*([\d.]+),\s*([\d.]+)", line)
    if m:
        server_state["tps"]   = float(m.group(1))
        server_state["tps5"]  = float(m.group(2))
        server_state["tps15"] = float(m.group(3))
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
        log("[Panel] ✅ Minecraft Server hazır! Oyuncular bağlanabilir.")
        threading.Thread(target=_tps_monitor, daemon=True).start()
        return

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
#  MINECRAFT SERVER YÖNETİMİ
# ══════════════════════════════════════════════════════════════

def download_paper():
    import urllib.request as urlreq
    import ssl

    ctx = ssl.create_default_context()
    log("[Panel] 📥 Paper MC indiriliyor...")
    try:
        api_url = (
            f"https://api.papermc.io/v2/projects/paper"
            f"/versions/{MC_VERSION}/builds"
        )
        req = urlreq.Request(api_url, headers={"User-Agent": "MCPanel/2.0"})
        with urlreq.urlopen(req, timeout=20, context=ctx) as r:
            builds = json.loads(r.read()).get("builds", [])

        if not builds:
            raise ValueError("Build listesi boş")

        build    = builds[-1]["build"]
        jar_name = f"paper-{MC_VERSION}-{build}.jar"
        url = (
            f"https://api.papermc.io/v2/projects/paper"
            f"/versions/{MC_VERSION}/builds/{build}/downloads/{jar_name}"
        )
        log(f"[Panel] 📦 {jar_name} indiriliyor (build #{build})...")

        req2 = urlreq.Request(url, headers={"User-Agent": "MCPanel/2.0"})
        done = 0
        with urlreq.urlopen(req2, timeout=180, context=ctx) as r2:
            total = int(r2.headers.get("Content-Length", 0))
            with open(MC_JAR, "wb") as f:
                while True:
                    chunk = r2.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 100 / total)
                        socketio.emit("download_progress",
                                      {"pct": pct, "done": done, "total": total})

        log(f"[Panel] ✅ Paper MC {MC_VERSION} build #{build} indirildi "
            f"({done // 1024 // 1024} MB)")
        return True

    except Exception as e:
        log(f"[Panel] ❌ İndirme hatası: {e}")
        return False


def write_server_config():
    (MC_DIR / "eula.txt").write_text("eula=true\n")

    props_file = MC_DIR / "server.properties"
    if not props_file.exists():
        props_file.write_text(
            f"server-port={MC_PORT}\n"
            "max-players=20\n"
            "online-mode=false\n"
            "gamemode=survival\n"
            "difficulty=normal\n"
            "level-name=world\n"
            "motd=\\u00A7a\\u00A7lLinux Masaüstü \\u00A7r\\u00A7fMC Server\n"
            "view-distance=8\n"
            "simulation-distance=6\n"
            "spawn-protection=0\n"
            "allow-flight=true\n"
            "enable-rcon=false\n"
            "max-tick-time=60000\n"
            "white-list=false\n"
            "enable-command-block=true\n"
            "enforce-whitelist=false\n"
            "pvp=true\n"
            "generate-structures=true\n"
            "allow-nether=true\n"
            "enable-query=false\n"
            "sync-chunk-writes=true\n"
        )

    config_dir = MC_DIR / "config"
    config_dir.mkdir(exist_ok=True)

    paper_world = config_dir / "paper-world-defaults.yml"
    if not paper_world.exists():
        paper_world.write_text(
            "world-settings:\n"
            "  default:\n"
            "    spawn-limits:\n"
            "      monsters: 70\n"
            "      animals: 10\n"
            "      water-animals: 5\n"
            "      water-ambient: 20\n"
            "    chunks:\n"
            "      auto-save-interval: 6000\n"
            "    entity-per-chunk-save-limit:\n"
            "      experience_orb: 16\n"
            "      snowball: 8\n"
            "      ender_pearl: 8\n"
            "      arrow: 16\n"
        )


def _setup_swap():
    """
    Tüm cgroup/bellek limitleri kaldır + disk → swap.
    main.py zaten boot'ta yapıyor; panel yeniden başlarsa diye burada da çalışır.
    """
    import psutil, subprocess, os, glob

    def _w(path, val):
        try:
            with open(path, "w") as f: f.write(str(val))
            return True
        except Exception:
            return False

    try:
        # ── cgroup v2 ─────────────────────────────────────────
        for path, val in [
            ("/sys/fs/cgroup/memory.max",      "max"),
            ("/sys/fs/cgroup/memory.swap.max", "max"),
            ("/sys/fs/cgroup/memory.high",     "max"),
            ("/sys/fs/cgroup/cpu.max",         "max"),
            ("/sys/fs/cgroup/pids.max",        "max"),
        ]:
            _w(path, val)
        for cg_dir in glob.glob("/sys/fs/cgroup/*/") + glob.glob("/sys/fs/cgroup/*/*/"):
            for fn, v in [("memory.max","max"),("memory.swap.max","max"),
                          ("memory.high","max"),("cpu.max","max"),("pids.max","max")]:
                _w(cg_dir + fn, v)

        # ── cgroup v1 ─────────────────────────────────────────
        for path, val in [
            ("/sys/fs/cgroup/memory/memory.limit_in_bytes",       "-1"),
            ("/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes", "-1"),
            ("/sys/fs/cgroup/memory/memory.soft_limit_in_bytes",  "-1"),
            ("/sys/fs/cgroup/memory/memory.swappiness",           "100"),
            ("/sys/fs/cgroup/memory/memory.oom_control",          "0"),
            ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us",               "-1"),
            ("/sys/fs/cgroup/pids/pids.max",                      "max"),
        ]:
            _w(path, val)
        for cg_dir in glob.glob("/sys/fs/cgroup/memory/*/"):
            _w(cg_dir + "memory.limit_in_bytes",       "-1")
            _w(cg_dir + "memory.memsw.limit_in_bytes", "-1")
            _w(cg_dir + "memory.swappiness",           "100")
            _w(cg_dir + "memory.oom_control",          "0")
        for cg_dir in glob.glob("/sys/fs/cgroup/cpu/*/"):
            _w(cg_dir + "cpu.cfs_quota_us", "-1")

        log("[Panel] 🔓 cgroup tüm limitler kaldırıldı")

        # ── Swap zaten yeterliyse atla ─────────────────────────
        swp = psutil.swap_memory()
        disk = psutil.disk_usage("/")
        free_gb = disk.free / 1024 / 1024 / 1024
        swap_gb = min(64, int(free_gb * 0.80))
        swap_mb = swap_gb * 1024

        if swp.total >= swap_mb * 1024 * 1024 * 0.9:
            log(f"[Panel] ✅ Swap zaten aktif: {swp.total//1024//1024}MB")
        else:
            swap_file = "/swapfile"
            log(f"[Panel] 💾 Swap oluşturuluyor: {swap_gb}GB...")

            if os.path.exists(swap_file):
                subprocess.run(["swapoff", swap_file], capture_output=True)
                try: os.remove(swap_file)
                except: pass

            ret = subprocess.run(
                ["fallocate", "-l", f"{swap_mb}M", swap_file],
                capture_output=True
            )
            if ret.returncode != 0:
                subprocess.run([
                    "dd", "if=/dev/zero", f"of={swap_file}",
                    "bs=64M", f"count={max(1, swap_mb//64)}", "status=none"
                ], capture_output=True)

            subprocess.run(["chmod", "600", swap_file], capture_output=True)
            subprocess.run(["mkswap", "-f", swap_file], capture_output=True)
            subprocess.run(["swapon", "-p", "0", swap_file], capture_output=True)

            # zram
            subprocess.run(["modprobe", "zram", "num_devices=1"], capture_output=True)
            zram_mb = min(4096, psutil.virtual_memory().total // 1024 // 1024)
            _w("/sys/block/zram0/comp_algorithm", "lz4")
            if _w("/sys/block/zram0/disksize", f"{zram_mb}M"):
                subprocess.run(["mkswap", "/dev/zram0"], capture_output=True)
                subprocess.run(["swapon", "-p", "100", "/dev/zram0"], capture_output=True)

        # ── VM ayarları ────────────────────────────────────────
        for path, val in [
            ("/proc/sys/vm/swappiness",             "200"),
            ("/proc/sys/vm/vfs_cache_pressure",     "500"),
            ("/proc/sys/vm/overcommit_memory",      "1"),
            ("/proc/sys/vm/overcommit_ratio",       "100"),
            ("/proc/sys/vm/page-cluster",           "0"),
            ("/proc/sys/vm/watermark_boost_factor", "0"),
            ("/proc/sys/vm/drop_caches",            "3"),
        ]:
            if not _w(path, val) and "swappiness" in path:
                _w(path, "100")

        swp2 = psutil.swap_memory()
        mem  = psutil.virtual_memory()
        log(f"[Panel] ✅ RAM={mem.total//1024//1024}MB + Swap={swp2.total//1024//1024}MB "
            f"= {(mem.total+swp2.total)//1024//1024}MB toplam")

    except Exception as e:
        log(f"[Panel] ⚠️  Swap/cgroup hatası: {e}")


def _ram_watchdog():
    """
    RAM + Swap watchdog.
    cgroup limiti kaldırıldı, swap aktif → 512MB kısıtı yok.
    Sadece toplam boş alan azaldığında entity temizle.
    """
    import psutil
    last_warn = 0

    while True:
        eventlet.sleep(5)
        try:
            mem  = psutil.virtual_memory()
            swp  = psutil.swap_memory()
            avail_mb = int((mem.available + swp.free) / 1024 / 1024)
            import time

            if avail_mb < 512:
                # Kritik: 512MB'den az boş → agresif temizlik
                try:
                    with open("/proc/sys/vm/drop_caches", "w") as f:
                        f.write("3")
                except Exception:
                    pass
                send_command("kill @e[type=item]")
                send_command("kill @e[type=experience_orb]")
                send_command("kill @e[type=arrow]")
                send_command("save-all")

            elif avail_mb < 1024:
                # Uyarı: 1GB'dan az kaldı
                now = time.time()
                if now - last_warn > 60:
                    send_command("kill @e[type=item]")
                    send_command("save-all")
                    last_warn = now

        except Exception:
            pass


def get_jvm_args():
    """
    Strateji: Fiziksel RAM (RSS) düşük tut → Render 512MB limitini aşma.
    Ama swap üzerinden büyük heap kullan → Minecraft rahat çalışsın.
    - Xms=64M  → JVM başlangıçta az fiziksel RAM alır
    - Xmx=büyük → heap swap'a taşabilir
    - PeriodicGC → JVM heap'i OS'a geri verir → RSS düşer
    """
    import psutil
    mem      = psutil.virtual_memory()
    swp      = psutil.swap_memory()
    total_mb = int(mem.total / 1024 / 1024)
    swap_mb  = int(swp.total / 1024 // 1024)

    # Xmx = fiziksel RAM'in %50'si + swap'ın %70'i, max 12GB
    xmx_mb = min(12288, int(total_mb * 0.50) + int(swap_mb * 0.70))
    xmx_mb = max(512, xmx_mb)
    xms_mb = 64   # KÜÇÜK başlat — fiziksel RAM önceden ayrılmaz

    xmx = f"{xmx_mb}M"
    xms = f"{xms_mb}M"
    log(f"[Panel] 🧠 RAM={total_mb}MB Swap={swap_mb}MB → Xms={xms} Xmx={xmx}")

    return [
        "java",
        f"-Xms{xms}", f"-Xmx{xmx}",

        # ── Compressed sınıflar küçük tut ─────────────────────
        "-XX:CompressedClassSpaceSize=128m",
        "-XX:MaxMetaspaceSize=256m",

        # ── G1GC + Heap'i OS'a geri ver (kritik!) ─────────────
        "-XX:+UseG1GC",
        "-XX:+ParallelRefProcEnabled",
        "-XX:MaxGCPauseMillis=200",
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+DisableExplicitGC",

        # Periyodik GC: boş heap sayfalarını OS'a geri ver → RSS düşer
        "-XX:G1PeriodicGCInterval=15000",
        "-XX:G1PeriodicGCSysLoadThreshold=0.0",
        "-XX:+G1PeriodicGCInvokesConcurrent",

        # Heap bölge boyutu
        "-XX:G1NewSizePercent=20",
        "-XX:G1MaxNewSizePercent=30",
        "-XX:G1HeapRegionSize=4m",
        "-XX:G1ReservePercent=20",
        "-XX:InitiatingHeapOccupancyPercent=15",
        "-XX:G1MixedGCCountTarget=8",
        "-XX:G1MixedGCLiveThresholdPercent=85",
        "-XX:G1HeapWastePercent=5",

        # Agresif bellek geri alma
        "-XX:SoftRefLRUPolicyMSPerMB=0",
        "-XX:SurvivorRatio=32",
        "-XX:MaxTenuringThreshold=1",
        "-XX:+UseStringDeduplication",

        # Genel
        "-Djava.net.preferIPv4Stack=true",
        "-Dfile.encoding=UTF-8",
        "-Duser.timezone=Europe/Istanbul",
        "-Dpaper.playerconnection.keepAlive=60",
        "-Dcom.mojang.eula.agree=true",
        "-jar", str(MC_JAR), "--nogui",
    ]


def start_server():
    global mc_process
    if mc_process and mc_process.poll() is None:
        return False, "Server zaten çalışıyor"

    MC_DIR.mkdir(parents=True, exist_ok=True)

    # ── Swap kurulumu — disk alanını RAM'e çevir ──────────────
    _setup_swap()

    if not MC_JAR.exists():
        server_state["status"] = "downloading"
        socketio.emit("server_status", server_state)
        if not download_paper():
            server_state["status"] = "stopped"
            socketio.emit("server_status", server_state)
            return False, "Jar indirilemedi"

    write_server_config()

    server_state["status"] = "starting"
    server_state["online_players"] = 0
    players.clear()
    socketio.emit("server_status", server_state)
    socketio.emit("players_update", [])

    jvm = get_jvm_args()
    log(f"[Panel] 🚀 Server başlatılıyor (RAM: {MC_RAM})...")
    log(f"[Panel] JVM: {' '.join(jvm[:6])} ...")

    try:
        mc_process = subprocess.Popen(
            jvm, cwd=str(MC_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        log(f"[Panel] ❌ Başlatma hatası: {e}")
        server_state["status"] = "stopped"
        socketio.emit("server_status", server_state)
        return False, str(e)

    threading.Thread(target=_stdout_reader, daemon=True).start()
    return True, "Başlatılıyor..."


def stop_server(force=False):
    global mc_process
    if not mc_process or mc_process.poll() is not None:
        return False, "Server çalışmıyor"
    server_state["status"] = "stopping"
    socketio.emit("server_status", server_state)
    if force:
        mc_process.kill()
        log("[Panel] ⚠️  Server zorla kapatıldı")
    else:
        send_command("save-all")
        time.sleep(1)
        send_command("stop")
    return True, "Durduruluyor..."


def send_command(cmd: str) -> bool:
    if mc_process and mc_process.poll() is None:
        try:
            mc_process.stdin.write(f"{cmd}\n".encode())
            mc_process.stdin.flush()
            return True
        except Exception:
            pass
    return False


# ══════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════

@app.route("/api/start", methods=["POST"])
def api_start():
    ok, msg = start_server()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    force = (request.json or {}).get("force", False)
    ok, msg = stop_server(force)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    stop_server()
    time.sleep(4)
    ok, msg = start_server()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/status")
def api_status():
    return jsonify({**server_state,
                    "players": _players_list(),
                    "tunnel": tunnel_info})


@app.route("/api/command", methods=["POST"])
def api_command():
    cmd = (request.json or {}).get("cmd", "").strip()
    if not cmd:
        return jsonify({"ok": False})
    ok = send_command(cmd)
    return jsonify({"ok": ok})


@app.route("/api/console/history")
def api_console_history():
    return jsonify(list(console_buf))


@app.route("/api/internal/tunnel", methods=["POST"])
def api_internal_tunnel():
    d = request.json or {}
    tunnel_info["url"]  = d.get("url", "")
    tunnel_info["host"] = d.get("host", "")
    socketio.emit("tunnel_update", tunnel_info)
    return jsonify({"ok": True})


@app.route("/api/tunnel")
def api_tunnel():
    return jsonify(tunnel_info)


@app.route("/api/players")
def api_players():
    send_command("list")
    return jsonify({"players": _players_list(), "count": len(players)})


@app.route("/api/players/kick", methods=["POST"])
def api_kick():
    d = request.json or {}
    send_command(f"kick {d['player']} {d.get('reason', 'Kicked by admin')}")
    return jsonify({"ok": True})


@app.route("/api/players/ban", methods=["POST"])
def api_ban():
    d = request.json or {}
    send_command(f"ban {d['player']} {d.get('reason', 'Banned by admin')}")
    return jsonify({"ok": True})


@app.route("/api/players/ban-ip", methods=["POST"])
def api_ban_ip():
    send_command(f"ban-ip {(request.json or {})['player']}")
    return jsonify({"ok": True})


@app.route("/api/players/pardon", methods=["POST"])
def api_pardon():
    send_command(f"pardon {(request.json or {})['player']}")
    return jsonify({"ok": True})


@app.route("/api/players/op", methods=["POST"])
def api_op():
    send_command(f"op {(request.json or {})['player']}")
    return jsonify({"ok": True})


@app.route("/api/players/deop", methods=["POST"])
def api_deop():
    send_command(f"deop {(request.json or {})['player']}")
    return jsonify({"ok": True})


@app.route("/api/players/gamemode", methods=["POST"])
def api_gamemode():
    d = request.json or {}
    send_command(f"gamemode {d['mode']} {d['player']}")
    return jsonify({"ok": True})


@app.route("/api/players/tp", methods=["POST"])
def api_tp():
    d = request.json or {}
    dest = d.get("to") or d.get("player")
    send_command(f"tp {d['player']} {dest}")
    return jsonify({"ok": True})


@app.route("/api/players/give", methods=["POST"])
def api_give():
    d = request.json or {}
    send_command(f"give {d['player']} {d['item']} {d.get('count', 1)}")
    return jsonify({"ok": True})


@app.route("/api/players/msg", methods=["POST"])
def api_msg():
    d = request.json or {}
    send_command(f"tell {d['player']} {d['message']}")
    return jsonify({"ok": True})


@app.route("/api/players/heal", methods=["POST"])
def api_heal():
    p = (request.json or {}).get("player", "@a")
    send_command(f"effect give {p} regeneration 5 255 true")
    send_command(f"effect give {p} saturation 5 255 true")
    return jsonify({"ok": True})


@app.route("/api/players/kill", methods=["POST"])
def api_kill_player():
    send_command(f"kill {(request.json or {}).get('player', '')}")
    return jsonify({"ok": True})


@app.route("/api/banlist")
def api_banlist():
    f = MC_DIR / "banned-players.json"
    return jsonify(json.loads(f.read_text()) if f.exists() else [])


@app.route("/api/whitelist")
def api_whitelist():
    f = MC_DIR / "whitelist.json"
    return jsonify(json.loads(f.read_text()) if f.exists() else [])


@app.route("/api/whitelist/add", methods=["POST"])
def api_whitelist_add():
    send_command(f"whitelist add {(request.json or {})['player']}")
    return jsonify({"ok": True})


@app.route("/api/whitelist/remove", methods=["POST"])
def api_whitelist_remove():
    send_command(f"whitelist remove {(request.json or {})['player']}")
    return jsonify({"ok": True})


@app.route("/api/whitelist/toggle", methods=["POST"])
def api_whitelist_toggle():
    on = (request.json or {}).get("on", True)
    send_command("whitelist on" if on else "whitelist off")
    return jsonify({"ok": True})


# ── Dosya yönetimi ────────────────────────────────────────────

def safe_path(rel: str) -> Path:
    p = (MC_DIR / rel).resolve()
    if not str(p).startswith(str(MC_DIR.resolve())):
        abort(403)
    return p


@app.route("/api/files")
def api_files():
    p = safe_path(request.args.get("path", ""))
    if not p.exists():
        return jsonify([])
    items = []
    for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        stat = item.stat()
        size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file()) if item.is_dir() else stat.st_size
        items.append({
            "name":     item.name,
            "path":     str(item.relative_to(MC_DIR)),
            "type":     "dir" if item.is_dir() else "file",
            "size":     size,
            "modified": int(stat.st_mtime),
            "ext":      item.suffix.lower() if item.is_file() else "",
        })
    return jsonify(items)


@app.route("/api/files/read")
def api_file_read():
    p = safe_path(request.args.get("path", ""))
    if not p.is_file():
        abort(404)
    try:
        return jsonify({"content": p.read_text(errors="replace"),
                        "path": str(p.relative_to(MC_DIR))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/files/write", methods=["POST"])
def api_file_write():
    d = request.json or {}
    p = safe_path(d["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(d["content"])
    return jsonify({"ok": True})


@app.route("/api/files/delete", methods=["POST"])
def api_file_delete():
    p = safe_path((request.json or {})["path"])
    shutil.rmtree(p) if p.is_dir() else p.unlink()
    return jsonify({"ok": True})


@app.route("/api/files/mkdir", methods=["POST"])
def api_mkdir():
    safe_path((request.json or {})["path"]).mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True})


@app.route("/api/files/rename", methods=["POST"])
def api_rename():
    d = request.json or {}
    safe_path(d["from"]).rename(safe_path(d["to"]))
    return jsonify({"ok": True})


@app.route("/api/files/upload", methods=["POST"])
def api_upload():
    rel = request.form.get("path", "")
    for fname, f in request.files.items():
        p = safe_path(rel + "/" + f.filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        f.save(str(p))
    return jsonify({"ok": True})


@app.route("/api/files/download")
def api_download():
    p = safe_path(request.args.get("path", ""))
    if not p.exists():
        abort(404)
    if p.is_dir():
        zp = f"/tmp/{p.name}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for fp in p.rglob("*"):
                if fp.is_file():
                    z.write(fp, fp.relative_to(p))
        return send_file(zp, as_attachment=True, download_name=f"{p.name}.zip")
    return send_file(str(p), as_attachment=True)


# ── Plugin yönetimi ───────────────────────────────────────────

@app.route("/api/plugins")
def api_plugins():
    pdir = MC_DIR / "plugins"
    pdir.mkdir(exist_ok=True)
    result = []
    for jar in sorted(pdir.glob("*.jar")):
        result.append({
            "name":    jar.stem,
            "file":    jar.name,
            "size":    jar.stat().st_size,
            "enabled": True,
        })
    for jar in sorted(pdir.glob("*.jar.disabled")):
        result.append({
            "name":    jar.name.replace(".jar.disabled", ""),
            "file":    jar.name,
            "size":    jar.stat().st_size,
            "enabled": False,
        })
    return jsonify(result)


@app.route("/api/plugins/upload", methods=["POST"])
def api_plugin_upload():
    pdir = MC_DIR / "plugins"
    pdir.mkdir(exist_ok=True)
    uploaded = []
    for f in request.files.values():
        if f.filename.endswith(".jar"):
            dest = pdir / f.filename
            f.save(str(dest))
            uploaded.append(f.filename)
    return jsonify({"ok": True, "uploaded": uploaded,
                    "msg": f"{len(uploaded)} plugin yüklendi. Yeniden başlatın."})


@app.route("/api/plugins/delete", methods=["POST"])
def api_plugin_delete():
    p = MC_DIR / "plugins" / (request.json or {})["file"]
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


@app.route("/api/plugins/toggle", methods=["POST"])
def api_plugin_toggle():
    name = (request.json or {})["file"]
    p    = MC_DIR / "plugins" / name
    if name.endswith(".disabled"):
        new = MC_DIR / "plugins" / name[:-len(".disabled")]
    else:
        new = MC_DIR / "plugins" / (name + ".disabled")
    if p.exists():
        p.rename(new)
    return jsonify({"ok": True})


@app.route("/api/plugins/search")
def api_plugin_search():
    q = request.args.get("q", "")
    try:
        r = requests.get(
            f"https://hangar.papermc.io/api/v1/projects?q={q}&limit=12",
            timeout=10
        )
        data = r.json()
        out  = []
        for p in data.get("result", []):
            ns = p.get("namespace", {})
            out.append({
                "name":      p.get("name", ""),
                "description": p.get("description", "")[:120],
                "downloads": p.get("stats", {}).get("downloads", 0),
                "url": f"https://hangar.papermc.io/{ns.get('owner','')}/{p.get('name','')}",
                "owner":     ns.get("owner", ""),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Sunucu ayarları ───────────────────────────────────────────

@app.route("/api/settings")
def api_settings():
    f = MC_DIR / "server.properties"
    if not f.exists():
        return jsonify({})
    props = {}
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return jsonify(props)


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    f = MC_DIR / "server.properties"
    existing = {}
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing.update(request.json or {})
    lines = [f"# Güncellendi: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    for k, v in existing.items():
        lines.append(f"{k}={v}")
    f.write_text("\n".join(lines) + "\n")
    return jsonify({"ok": True, "msg": "Kaydedildi. Yeniden başlatın."})


# ── Dünya yönetimi ────────────────────────────────────────────

@app.route("/api/worlds")
def api_worlds():
    worlds = []
    for d in MC_DIR.iterdir():
        if d.is_dir() and (d / "level.dat").exists():
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            worlds.append({
                "name":     d.name,
                "size":     size,
                "modified": int(d.stat().st_mtime),
            })
    return jsonify(worlds)


@app.route("/api/worlds/backup", methods=["POST"])
def api_world_backup():
    world = (request.json or {}).get("world", "world")
    src   = MC_DIR / world
    if not src.exists():
        return jsonify({"ok": False, "error": "Dünya bulunamadı"})
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = MC_DIR / "backups" / f"{world}_{ts}.zip"
    dest.parent.mkdir(exist_ok=True)
    send_command("save-off")
    time.sleep(1)
    send_command("save-all")
    time.sleep(2)
    with zipfile.ZipFile(str(dest), "w", zipfile.ZIP_DEFLATED) as z:
        for fp in src.rglob("*"):
            if fp.is_file():
                z.write(fp, fp.relative_to(MC_DIR))
    send_command("save-on")
    size = dest.stat().st_size
    return jsonify({"ok": True,
                    "file": str(dest.relative_to(MC_DIR)),
                    "size": size})


@app.route("/api/worlds/delete", methods=["POST"])
def api_world_delete():
    world = (request.json or {}).get("world")
    if not world:
        return jsonify({"ok": False})
    p = MC_DIR / world
    if p.exists() and (p / "level.dat").exists():
        shutil.rmtree(p)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Dünya bulunamadı"})


@app.route("/api/backups")
def api_backups():
    bdir = MC_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    items = []
    for f in sorted(bdir.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        items.append({
            "name":    f.name,
            "path":    str(f.relative_to(MC_DIR)),
            "size":    f.stat().st_size,
            "created": int(f.stat().st_mtime),
        })
    return jsonify(items)


# ── Performans ────────────────────────────────────────────────

@app.route("/api/performance")
def api_performance():
    try:
        import psutil
        cpu = psutil.cpu_percent(0.2)
        vm  = psutil.virtual_memory()
        dk  = psutil.disk_usage("/")
        procs = []
        if mc_process and mc_process.poll() is None:
            try:
                proc = psutil.Process(mc_process.pid)
                procs = [{
                    "cpu": round(proc.cpu_percent(), 1),
                    "ram": int(proc.memory_info().rss / 1024 / 1024),
                    "threads": proc.num_threads(),
                }]
            except Exception:
                pass
        return jsonify({
            "cpu":          round(cpu, 1),
            "ram_pct":      round(vm.percent, 1),
            "ram_used_mb":  int(vm.used    / 1024 / 1024),
            "ram_total_mb": int(vm.total   / 1024 / 1024),
            "ram_free_mb":  int(vm.available/1024 / 1024),
            "disk_pct":     round(dk.percent, 1),
            "disk_used_gb": round(dk.used  / 1e9, 1),
            "disk_total_gb":round(dk.total / 1e9, 1),
            "cpu_count":    psutil.cpu_count(),
            "mc":           procs[0] if procs else {},
            "tps":          server_state["tps"],
            "tps5":         server_state["tps5"],
            "tps15":        server_state["tps15"],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ── SocketIO events ──────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("console_history", list(console_buf))
    emit("server_status",   server_state)
    emit("players_update",  _players_list())
    emit("tunnel_update",   tunnel_info)


@socketio.on("send_command")
def on_send_command(data):
    cmd = (data or {}).get("cmd", "").strip()
    if cmd:
        ok = send_command(cmd)
        if not ok:
            emit("console_line", {"ts": datetime.now().strftime("%H:%M:%S"),
                                   "line": "[Panel] ⚠️  Server çalışmıyor, komut gönderilemedi"})


# ══════════════════════════════════════════════════════════════
#  PANEL HTML
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return PANEL_HTML


PANEL_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⛏️ Minecraft Yönetim Paneli</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@300;400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #05060c; --s1: #0b0d16; --s2: #0f1120; --s3: #131627;
  --a1: #00e5ff; --a2: #7c6aff; --a3: #00ffaa; --a4: #ff6b35;
  --red: #ff4757; --green: #2ed573; --yellow: #ffa502; --orange: #ff6b35;
  --t1: #eef0f8; --t2: #8892a4; --t3: #3d4558;
  --font: 'Sora', sans-serif; --mono: 'JetBrains Mono', monospace;
  --sidebar: 230px; --r: 12px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--bg); color: var(--t1); font-family: var(--font); overflow: hidden; }
.layout { display: flex; height: 100vh; }
.sidebar {
  width: var(--sidebar); background: var(--s1);
  border-right: 1px solid rgba(255,255,255,.06);
  display: flex; flex-direction: column; flex-shrink: 0; overflow-y: auto;
}
.sb-head { padding: 18px 16px 14px; border-bottom: 1px solid rgba(255,255,255,.06); }
.sb-head h2 { font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.sb-ver { font-size: 10px; color: var(--t2); font-family: var(--mono); margin-top: 4px; }
.sb-status {
  margin: 10px 10px 0;
  background: rgba(255,255,255,.03); border-radius: 9px;
  padding: 9px 12px; display: flex; align-items: center; gap: 8px; font-size: 12px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot-green  { background: var(--green);  box-shadow: 0 0 6px var(--green); }
.dot-red    { background: var(--red);    box-shadow: 0 0 6px var(--red); }
.dot-yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); animation: blink 1s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }
.nav { padding: 10px; flex: 1; }
.nav-sec { font-size: 9px; font-weight: 700; color: var(--t3); text-transform: uppercase;
  letter-spacing: .12em; padding: 10px 6px 5px; }
.nav-item {
  display: flex; align-items: center; gap: 9px;
  padding: 8px 10px; border-radius: 9px; cursor: pointer;
  transition: all .15s; font-size: 13px; color: var(--t2); margin-bottom: 1px;
}
.nav-item:hover { background: rgba(255,255,255,.05); color: var(--t1); }
.nav-item.active { background: rgba(0,229,255,.09); color: var(--a1); font-weight: 600; }
.nav-item .ico { font-size: 15px; width: 18px; text-align: center; }
.sb-ctrl { padding: 12px 10px; border-top: 1px solid rgba(255,255,255,.06); }
.ctrl-btn {
  width: 100%; padding: 8px; border-radius: 9px; font-size: 12px; font-weight: 600;
  border: none; cursor: pointer; font-family: var(--font); transition: all .15s;
  margin-bottom: 6px; display: flex; align-items: center; justify-content: center; gap: 6px;
}
.cb-start   { background: linear-gradient(135deg, #2ed573, #00a550); color: #000; }
.cb-restart { background: rgba(255,165,2,.12); color: var(--yellow); border: 1px solid rgba(255,165,2,.25); }
.cb-stop    { background: rgba(255,71,87,.12); color: var(--red);    border: 1px solid rgba(255,71,87,.25); }
.ctrl-btn:hover { transform: translateY(-1px); filter: brightness(1.1); }
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.topbar {
  height: 50px; background: var(--s1); border-bottom: 1px solid rgba(255,255,255,.06);
  display: flex; align-items: center; padding: 0 18px; gap: 14px; flex-shrink: 0;
}
.page-title { font-size: 14px; font-weight: 700; flex: 1; }
.top-stats { display: flex; gap: 18px; font-size: 11px; color: var(--t2); font-family: var(--mono); }
.ts { display: flex; align-items: center; gap: 4px; }
.ts-v { color: var(--t1); font-weight: 600; }
.mc-addr-bar {
  background: linear-gradient(90deg, rgba(0,255,170,.08), rgba(0,229,255,.06));
  border-bottom: 1px solid rgba(0,255,170,.15);
  padding: 7px 18px; display: flex; align-items: center; gap: 10px;
  font-size: 12px; flex-shrink: 0;
}
.mc-addr-bar .lbl { color: var(--t2); }
.mc-addr-bar .addr { color: var(--a3); font-family: var(--mono); font-weight: 600; }
.mc-addr-bar.hidden { display: none; }
.pages { flex: 1; overflow: hidden; }
.page  { display: none; height: 100%; overflow-y: auto; padding: 18px; }
.page.active { display: block; }
.card {
  background: var(--s1); border: 1px solid rgba(255,255,255,.06);
  border-radius: var(--r); padding: 18px; margin-bottom: 14px;
}
.card-hd {
  font-size: 11px; font-weight: 700; color: var(--t2); text-transform: uppercase;
  letter-spacing: .1em; margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
}
.card-hd::before { content:''; width: 3px; height: 11px; border-radius: 2px; background: var(--a1); }
.g2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.g3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
.g4 { display: grid; grid-template-columns: repeat(4,1fr); gap: 12px; }
.sc { background: var(--s2); border: 1px solid rgba(255,255,255,.05); border-radius: 10px; padding: 16px; text-align: center; }
.sc-val { font-size: 26px; font-weight: 700; font-family: var(--mono);
  background: linear-gradient(135deg, var(--a1), var(--a2));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.sc-lbl { font-size: 11px; color: var(--t2); margin-top: 4px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.tbl th { padding: 8px 10px; text-align: left; font-size: 10px; font-weight: 700;
  color: var(--t2); text-transform: uppercase; letter-spacing: .08em;
  border-bottom: 1px solid rgba(255,255,255,.06); }
.tbl td { padding: 9px 10px; border-bottom: 1px solid rgba(255,255,255,.04); }
.tbl tr:hover td { background: rgba(255,255,255,.02); }
.tbl tr:last-child td { border: none; }
.badge { border-radius: 20px; padding: 2px 8px; font-size: 10px; font-weight: 700; font-family: var(--mono); }
.bg  { background: rgba(46,213,115,.1);  border: 1px solid rgba(46,213,115,.25);  color: var(--green); }
.br  { background: rgba(255,71,87,.1);   border: 1px solid rgba(255,71,87,.25);   color: var(--red); }
.bb  { background: rgba(0,229,255,.08);  border: 1px solid rgba(0,229,255,.2);    color: var(--a1); }
.by  { background: rgba(255,165,2,.1);   border: 1px solid rgba(255,165,2,.25);   color: var(--yellow); }
.bo  { background: rgba(255,107,53,.1);  border: 1px solid rgba(255,107,53,.25);  color: var(--orange); }
.btn {
  padding: 6px 14px; border-radius: 7px; font-size: 12px; font-weight: 600;
  border: none; cursor: pointer; font-family: var(--font); transition: all .15s;
  display: inline-flex; align-items: center; gap: 5px; text-decoration: none; white-space: nowrap;
}
.btn:hover { transform: translateY(-1px); }
.btn-sm  { padding: 4px 10px; font-size: 11px; }
.btn-lg  { padding: 10px 22px; font-size: 13px; }
.b-prim  { background: linear-gradient(135deg, var(--a1), var(--a2)); color: #000; }
.b-prim:hover { box-shadow: 0 6px 20px rgba(0,229,255,.3); }
.b-dang  { background: rgba(255,71,87,.12);  color: var(--red);    border: 1px solid rgba(255,71,87,.25); }
.b-dang:hover { background: rgba(255,71,87,.25); }
.b-warn  { background: rgba(255,165,2,.1);   color: var(--yellow); border: 1px solid rgba(255,165,2,.25); }
.b-succ  { background: rgba(46,213,115,.1);  color: var(--green);  border: 1px solid rgba(46,213,115,.25); }
.b-ghost { background: rgba(255,255,255,.05); color: var(--t2);    border: 1px solid rgba(255,255,255,.1); }
.con-wrap { height: calc(100% - 42px); display: flex; flex-direction: column; }
.con-out {
  flex: 1; background: #000; border-radius: 10px; padding: 12px; overflow-y: auto;
  font-family: var(--mono); font-size: 11.5px; line-height: 1.65;
  border: 1px solid rgba(255,255,255,.06);
}
.con-out::-webkit-scrollbar { width: 5px; }
.con-out::-webkit-scrollbar-thumb { background: rgba(255,255,255,.1); border-radius: 3px; }
.cl-info  { color: #9cdcfe; }
.cl-warn  { color: #dcdcaa; }
.cl-err   { color: #f44747; }
.cl-panel { color: #00e5ff; }
.cl-def   { color: #d4d4d4; }
.cl-cmd   { color: #b5cea8; }
.con-in   { display: flex; gap: 8px; margin-top: 8px; }
.con-input {
  flex: 1; background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.1);
  color: var(--t1); border-radius: 8px; padding: 9px 12px; font-family: var(--mono);
  font-size: 12px; outline: none;
}
.con-input:focus { border-color: rgba(0,229,255,.4); }
.con-send {
  padding: 9px 20px; background: linear-gradient(135deg, var(--a1), var(--a2));
  color: #000; border: none; border-radius: 8px; font-weight: 700;
  cursor: pointer; font-family: var(--font); transition: all .15s;
}
.con-send:hover { transform: translateY(-1px); box-shadow: 0 4px 15px rgba(0,229,255,.3); }
.fm { display: flex; gap: 12px; height: calc(100vh - 160px); }
.fm-tree { width: 280px; flex-shrink: 0; display: flex; flex-direction: column;
  background: var(--s1); border: 1px solid rgba(255,255,255,.06); border-radius: var(--r); overflow: hidden; }
.fm-toolbar { padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,.06);
  display: flex; gap: 6px; align-items: center; }
.fm-bread { font-size: 11px; color: var(--t2); font-family: var(--mono); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fm-list { flex: 1; overflow-y: auto; }
.fm-item {
  display: flex; align-items: center; gap: 8px; padding: 7px 12px;
  cursor: pointer; border-bottom: 1px solid rgba(255,255,255,.03);
  transition: background .1s; font-size: 12px;
}
.fm-item:hover { background: rgba(255,255,255,.04); }
.fm-item.sel   { background: rgba(0,229,255,.07); border-left: 2px solid var(--a1); }
.fm-ico  { font-size: 14px; width: 18px; text-align: center; }
.fm-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fm-size { font-size: 10px; color: var(--t3); font-family: var(--mono); }
.fm-editor { flex: 1; display: flex; flex-direction: column;
  background: var(--s1); border: 1px solid rgba(255,255,255,.06); border-radius: var(--r); overflow: hidden; }
.fm-etool { padding: 9px 12px; border-bottom: 1px solid rgba(255,255,255,.06);
  display: flex; gap: 6px; align-items: center; }
.fm-fname { font-family: var(--mono); font-size: 11px; color: var(--t2); flex: 1; }
.fm-area {
  flex: 1; background: #1e1e1e; color: #d4d4d4; font-family: var(--mono);
  font-size: 12px; border: none; outline: none; padding: 14px; resize: none;
  line-height: 1.6;
}
.set-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 10px; }
.set-item { background: var(--s2); border: 1px solid rgba(255,255,255,.05); border-radius: 9px; padding: 12px; }
.set-lbl  { font-size: 10px; color: var(--t2); margin-bottom: 5px; text-transform: uppercase; letter-spacing: .06em; }
.set-inp  {
  width: 100%; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.1);
  color: var(--t1); border-radius: 7px; padding: 7px 9px; font-family: var(--mono);
  font-size: 12px; outline: none;
}
.set-inp:focus { border-color: rgba(0,229,255,.4); }
select.set-inp option { background: #1e1e1e; }
.prog { height: 5px; background: rgba(255,255,255,.06); border-radius: 3px; overflow: hidden; margin-top: 5px; }
.prog-f { height: 100%; border-radius: 3px; transition: width .5s; }
.pf-cpu  { background: linear-gradient(90deg, var(--a1), var(--a2)); }
.pf-ram  { background: linear-gradient(90deg, var(--a3), var(--a1)); }
.pf-disk { background: linear-gradient(90deg, var(--a2), var(--orange)); }
.inp {
  background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.1);
  color: var(--t1); border-radius: 8px; padding: 8px 12px; font-family: var(--font);
  font-size: 12px; outline: none;
}
.inp:focus { border-color: rgba(0,229,255,.4); }
.inp-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.modal-bg { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75); z-index: 100;
  align-items: center; justify-content: center; }
.modal-bg.open { display: flex; }
.modal { background: var(--s1); border: 1px solid rgba(255,255,255,.1); border-radius: 16px;
  padding: 24px; min-width: 360px; max-width: 500px; width: 90%; }
.modal h3 { font-size: 15px; font-weight: 700; margin-bottom: 14px; }
.modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
.m-inp { width: 100%; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.12);
  color: var(--t1); border-radius: 8px; padding: 9px 12px; font-family: var(--mono);
  font-size: 12px; outline: none; margin-bottom: 8px; }
.m-inp:focus { border-color: rgba(0,229,255,.4); }
.upzone { border: 2px dashed rgba(255,255,255,.12); border-radius: 10px; padding: 20px;
  text-align: center; cursor: pointer; color: var(--t2); font-size: 13px; transition: all .2s; }
.upzone:hover, .upzone.drag { border-color: rgba(0,229,255,.4); background: rgba(0,229,255,.04); }
.notif-wrap { position: fixed; top: 14px; right: 14px; z-index: 200; display: flex; flex-direction: column; gap: 8px; }
.notif { padding: 10px 16px; border-radius: 10px; font-size: 12px; font-weight: 600; max-width: 300px;
  animation: slide-in .3s ease; }
@keyframes slide-in { from { transform: translateX(120px); opacity: 0; } to { transform: none; opacity: 1; } }
.n-ok   { background: rgba(46,213,115,.15);  border: 1px solid rgba(46,213,115,.3);  color: var(--green); }
.n-err  { background: rgba(255,71,87,.15);   border: 1px solid rgba(255,71,87,.3);   color: var(--red); }
.n-info { background: rgba(0,229,255,.1);    border: 1px solid rgba(0,229,255,.25);  color: var(--a1); }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,.1); border-radius: 3px; }
input[type=file] { display: none; }
</style>
</head>
<body>
<div class="layout">
<div class="sidebar">
  <div class="sb-head">
    <h2>⛏️ MC Panel</h2>
    <div class="sb-ver" id="sb-ver">Paper MC • yükleniyor...</div>
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
    <div class="nav-item" data-page="files"><span class="ico">📁</span>Dosya Yöneticisi</div>
    <div class="nav-item" data-page="worlds"><span class="ico">🌍</span>Dünyalar</div>
    <div class="nav-item" data-page="backups"><span class="ico">💾</span>Yedekler</div>
    <div class="nav-item" data-page="settings"><span class="ico">⚙️</span>Ayarlar</div>
    <div class="nav-sec">İzleme</div>
    <div class="nav-item" data-page="perf"><span class="ico">📈</span>Performans</div>
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
      <div class="ts">👥 <span class="ts-v" id="tb-pl">0</span></div>
      <div class="ts">⚡ <span class="ts-v" id="tb-tps">20.0</span> TPS</div>
      <div class="ts">🧠 <span class="ts-v" id="tb-ram">— MB</span></div>
    </div>
  </div>
  <div class="mc-addr-bar hidden" id="mc-addr-bar">
    <span class="lbl">📌 MC Server Adresi:</span>
    <span class="addr" id="mc-addr-text">bekleniyor...</span>
    <button class="btn btn-sm b-ghost" onclick="copyAddr()">📋 Kopyala</button>
  </div>
  <div class="pages">
  <div class="page active" id="page-dashboard">
    <div class="g4" style="margin-bottom:14px">
      <div class="sc"><div class="sc-val" id="d-pl">0</div><div class="sc-lbl">👥 Online Oyuncu</div></div>
      <div class="sc"><div class="sc-val" id="d-tps">20.0</div><div class="sc-lbl">⚡ TPS (1dk)</div></div>
      <div class="sc"><div class="sc-val" id="d-ram">—</div><div class="sc-lbl">🧠 MC RAM (MB)</div></div>
      <div class="sc"><div class="sc-val" id="d-up">—</div><div class="sc-lbl">⏱ Çalışma Süresi</div></div>
    </div>
    <div class="g2">
      <div class="card">
        <div class="card-hd">Sunucu Bilgisi</div>
        <table class="tbl">
          <tr><td style="color:var(--t2)">Durum</td><td><span class="badge bg" id="d-status">—</span></td></tr>
          <tr><td style="color:var(--t2)">Versiyon</td><td id="d-ver">—</td></tr>
          <tr><td style="color:var(--t2)">MC RAM Limiti</td><td id="d-maxram">—</td></tr>
          <tr><td style="color:var(--t2)">TPS (1m/5m/15m)</td><td id="d-tps3">—</td></tr>
          <tr><td style="color:var(--t2)">Max Oyuncu</td><td id="d-maxpl">20</td></tr>
          <tr><td style="color:var(--t2)">Bağlantı</td><td><span id="d-addr" style="font-family:var(--mono);font-size:11px;color:var(--a3)">—</span></td></tr>
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
      <div id="d-log" style="font-family:var(--mono);font-size:11px;max-height:200px;overflow-y:auto;line-height:1.7;color:#9cdcfe"></div>
    </div>
  </div>
  <div class="page" id="page-console" style="height:100%;display:none;flex-direction:column;padding:14px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <div class="card-hd" style="margin:0">💻 Gerçek Zamanlı Konsol</div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-sm b-ghost" onclick="conClear()">🗑 Temizle</button>
        <button class="btn btn-sm b-ghost" onclick="conBottom()">↓ En Alta</button>
        <button class="btn btn-sm b-ghost" onclick="conSearch()">🔍 Ara</button>
      </div>
    </div>
    <div class="con-wrap" style="flex:1">
      <div class="con-out" id="con-out"></div>
      <div class="con-in">
        <input class="con-input" id="con-inp" placeholder="Komut gir... (list / tps / give / tp / gamemode / weather ...)">
        <button class="con-send" onclick="conSend()">▶ Gönder</button>
      </div>
    </div>
  </div>
  <div class="page" id="page-players">
    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">👥 Online Oyuncular</div>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm b-ghost" onclick="broadcast()">📢 Duyuru</button>
          <button class="btn btn-sm b-ghost" onclick="refreshPlayers()">↺ Yenile</button>
        </div>
      </div>
      <table class="tbl">
        <thead><tr><th>Oyuncu</th><th>Durum</th><th>İşlemler</th></tr></thead>
        <tbody id="pl-body"></tbody>
      </table>
    </div>
    <div class="g2">
      <div class="card">
        <div class="card-hd">⚡ Hızlı İşlem</div>
        <div class="inp-row">
          <input class="inp" id="pl-name" placeholder="Oyuncu adı" style="flex:1">
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">
          <button class="btn btn-sm b-dang" onclick="plAct('kick')">👢 Kick</button>
          <button class="btn btn-sm b-dang" onclick="plAct('ban')">🔨 Ban</button>
          <button class="btn btn-sm b-succ" onclick="plAct('op')">⭐ OP</button>
          <button class="btn btn-sm b-warn" onclick="plAct('deop')">✕ DeOP</button>
          <button class="btn btn-sm b-ghost" onclick="plAct('tp')">🚀 TP</button>
          <button class="btn btn-sm b-succ" onclick="plAct('heal')">❤️ Heal</button>
          <button class="btn btn-sm b-dang" onclick="plAct('kill')">💀 Kill</button>
        </div>
        <div style="font-size:11px;color:var(--t2);margin-bottom:6px">Oyun Modu:</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">
          <button class="btn btn-sm b-ghost" onclick="setGM('survival')">⚔️ Survival</button>
          <button class="btn btn-sm b-ghost" onclick="setGM('creative')">🎨 Creative</button>
          <button class="btn btn-sm b-ghost" onclick="setGM('adventure')">🗺️ Adventure</button>
          <button class="btn btn-sm b-ghost" onclick="setGM('spectator')">👁 Spectator</button>
        </div>
      </div>
      <div class="card">
        <div class="card-hd">📩 Mesaj & Give</div>
        <input class="inp" id="msg-pl" placeholder="Oyuncu" style="width:100%;margin-bottom:6px">
        <input class="inp" id="msg-txt" placeholder="Mesaj" style="width:100%;margin-bottom:6px">
        <button class="btn b-prim btn-sm" style="width:100%;margin-bottom:12px" onclick="sendMsg()">📩 Gönder</button>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <input class="inp" id="give-item" placeholder="Item (diamond_sword)" style="flex:2">
          <input class="inp" id="give-count" placeholder="Miktar" type="number" value="1" style="width:70px">
          <button class="btn b-prim btn-sm" onclick="giveItem()">🎁 Give</button>
        </div>
      </div>
    </div>
  </div>
  <div class="page" id="page-whitelist">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">📋 Beyaz Liste</div>
        <div class="inp-row" style="margin:0;gap:6px">
          <input class="inp" id="wl-name" placeholder="Oyuncu adı" style="width:160px">
          <button class="btn btn-sm b-succ" onclick="wlAdd()">+ Ekle</button>
          <button class="btn btn-sm b-ghost" onclick="api('/api/whitelist/toggle',{on:true});notify('Beyaz liste açıldı','ok')">Aç</button>
          <button class="btn btn-sm b-ghost" onclick="api('/api/whitelist/toggle',{on:false});notify('Beyaz liste kapatıldı','ok')">Kapat</button>
        </div>
      </div>
      <table class="tbl"><thead><tr><th>Oyuncu</th><th>UUID</th><th>İşlem</th></tr></thead>
        <tbody id="wl-body"></tbody></table>
    </div>
  </div>
  <div class="page" id="page-banlist">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">🔨 Ban Listesi</div>
        <div class="inp-row" style="margin:0;gap:6px">
          <input class="inp" id="ban-name" placeholder="Oyuncu adı" style="width:160px">
          <input class="inp" id="ban-reason" placeholder="Sebep" style="width:160px">
          <button class="btn btn-sm b-dang" onclick="banPlayer()">🔨 Ban Et</button>
        </div>
      </div>
      <table class="tbl"><thead><tr><th>Oyuncu</th><th>Sebep</th><th>Tarih</th><th>İşlem</th></tr></thead>
        <tbody id="ban-body"></tbody></table>
    </div>
  </div>
  <div class="page" id="page-plugins">
    <div class="g2" style="height:calc(100vh - 150px)">
      <div class="card" style="display:flex;flex-direction:column;overflow:hidden">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-shrink:0">
          <div class="card-hd" style="margin:0">🔌 Kurulu Pluginler</div>
          <div>
            <label for="plug-up" class="btn btn-sm b-prim" style="cursor:pointer">⬆ .jar Yükle</label>
            <input type="file" id="plug-up" accept=".jar" multiple onchange="uploadPlugin(this)">
            <button class="btn btn-sm b-ghost" onclick="loadPlugins()">↺</button>
          </div>
        </div>
        <div style="flex:1;overflow-y:auto">
          <table class="tbl" id="plug-tbl">
            <thead><tr><th>Plugin</th><th>Boyut</th><th>Durum</th><th>İşlem</th></tr></thead>
            <tbody id="plug-body"></tbody>
          </table>
        </div>
        <div style="padding-top:10px;flex-shrink:0">
          <div class="upzone" id="plug-zone"
            ondragover="event.preventDefault();this.classList.add('drag')"
            ondragleave="this.classList.remove('drag')"
            ondrop="dropPlugin(event)">
            📦 Plugin .jar dosyasını buraya sürükleyin
          </div>
        </div>
      </div>
      <div class="card" style="display:flex;flex-direction:column;overflow:hidden">
        <div class="card-hd">🔍 Plugin Market (Hangar)</div>
        <div style="display:flex;gap:6px;margin-bottom:12px;flex-shrink:0">
          <input class="inp" id="plug-q" placeholder="Ara: EssentialsX, WorldEdit, Vault..." style="flex:1">
          <button class="btn b-prim" onclick="searchPlugins()">Ara</button>
        </div>
        <div id="plug-results" style="flex:1;overflow-y:auto"></div>
      </div>
    </div>
  </div>
  <div class="page" id="page-files" style="height:100%;padding:14px">
    <div class="fm">
      <div class="fm-tree">
        <div class="fm-toolbar">
          <span class="fm-bread" id="fm-bread">/</span>
          <button class="btn btn-sm b-ghost" onclick="fmUp()" title="Üst dizin">↑</button>
          <button class="btn btn-sm b-ghost" onclick="fmRefresh()" title="Yenile">↺</button>
          <button class="btn btn-sm b-prim"  onclick="fmNewModal()" title="Yeni">+</button>
        </div>
        <div class="fm-list" id="fm-list"></div>
      </div>
      <div class="fm-editor">
        <div class="fm-etool">
          <span class="fm-fname" id="fm-fname">Dosya seçin...</span>
          <button class="btn btn-sm b-prim"  id="fm-save" onclick="fmSave()" disabled>💾 Kaydet</button>
          <button class="btn btn-sm b-ghost" onclick="fmDownload()">⬇ İndir</button>
          <label class="btn btn-sm b-ghost" style="cursor:pointer" title="Yükle">
            ⬆ Yükle <input type="file" multiple onchange="fmUpload(this)">
          </label>
          <button class="btn btn-sm b-dang"  onclick="fmDelete()">🗑 Sil</button>
        </div>
        <textarea class="fm-area" id="fm-area"
          placeholder="Düzenlemek için sol panelden bir dosya seçin..."
          oninput="document.getElementById('fm-save').disabled=false"></textarea>
      </div>
    </div>
  </div>
  <div class="page" id="page-worlds">
    <div class="card" style="margin-bottom:14px">
      <div class="card-hd">🌍 Dünya Listesi</div>
      <div id="worlds-list"><div style="color:var(--t2)">Yükleniyor...</div></div>
    </div>
    <div class="card">
      <div class="card-hd">⚡ Dünya Komutları</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <button class="btn b-ghost" onclick="cmd('time set day')">☀️ Gündüz</button>
        <button class="btn b-ghost" onclick="cmd('time set night')">🌙 Gece</button>
        <button class="btn b-ghost" onclick="cmd('time set noon')">🌤️ Öğlen</button>
        <button class="btn b-ghost" onclick="cmd('weather clear')">⛅ Açık</button>
        <button class="btn b-ghost" onclick="cmd('weather rain')">🌧️ Yağmur</button>
        <button class="btn b-ghost" onclick="cmd('weather thunder')">⛈️ Fırtına</button>
        <button class="btn b-ghost" onclick="cmd('difficulty peaceful')">😊 Peaceful</button>
        <button class="btn b-ghost" onclick="cmd('difficulty easy')">🟢 Easy</button>
        <button class="btn b-ghost" onclick="cmd('difficulty normal')">🟡 Normal</button>
        <button class="btn b-ghost" onclick="cmd('difficulty hard')">🔴 Hard</button>
        <button class="btn b-ghost" onclick="cmd('gamerule doDaylightCycle false')">🔒 Zaman Dondur</button>
        <button class="btn b-ghost" onclick="cmd('gamerule doWeatherCycle false')">🔒 Hava Dondur</button>
        <button class="btn b-ghost" onclick="cmd('save-all')">💾 Kaydet</button>
        <button class="btn b-ghost" onclick="cmd('kill @e[type=!player]')">⚡ Mob Temizle</button>
        <button class="btn b-ghost" onclick="cmd('kill @e[type=item]')">🧹 Drop Temizle</button>
      </div>
    </div>
  </div>
  <div class="page" id="page-backups">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div class="card-hd" style="margin:0">💾 Yedek Dosyaları</div>
        <button class="btn b-prim" onclick="loadBackups()">↺ Yenile</button>
      </div>
      <table class="tbl">
        <thead><tr><th>Dosya</th><th>Boyut</th><th>Tarih</th><th>İşlem</th></tr></thead>
        <tbody id="backup-body"></tbody>
      </table>
    </div>
  </div>
  <div class="page" id="page-settings">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div class="card-hd" style="margin:0">⚙️ server.properties</div>
        <button class="btn b-prim btn-lg" onclick="saveSettings()">💾 Kaydet & Yeniden Başlat</button>
      </div>
      <div class="set-grid" id="settings-grid">
        <div style="color:var(--t2);padding:10px">Yükleniyor...</div>
      </div>
    </div>
  </div>
  <div class="page" id="page-perf">
    <div class="g2" style="margin-bottom:14px">
      <div class="card">
        <div class="card-hd">💻 Sistem Kaynakları</div>
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px">
            <span>CPU</span><span id="p-cpu">—</span></div>
          <div class="prog"><div class="prog-f pf-cpu" id="pb-cpu" style="width:0%"></div></div>
        </div>
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px">
            <span>RAM</span><span id="p-ram">—</span></div>
          <div class="prog"><div class="prog-f pf-ram" id="pb-ram" style="width:0%"></div></div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2);margin-bottom:3px">
            <span>Disk</span><span id="p-disk">—</span></div>
          <div class="prog"><div class="prog-f pf-disk" id="pb-disk" style="width:0%"></div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-hd">⛏️ Minecraft Kaynakları</div>
        <table class="tbl">
          <tr><td style="color:var(--t2)">MC RAM</td><td id="p-mcram">—</td></tr>
          <tr><td style="color:var(--t2)">MC CPU</td><td id="p-mccpu">—</td></tr>
          <tr><td style="color:var(--t2)">MC Thread</td><td id="p-mcthr">—</td></tr>
          <tr><td style="color:var(--t2)">TPS (1m)</td><td id="p-tps1">—</td></tr>
          <tr><td style="color:var(--t2)">TPS (5m)</td><td id="p-tps5">—</td></tr>
          <tr><td style="color:var(--t2)">TPS (15m)</td><td id="p-tps15">—</td></tr>
          <tr><td style="color:var(--t2)">Oyuncu</td><td id="p-pls">—</td></tr>
        </table>
        <button class="btn b-ghost" style="margin-top:10px;width:100%" onclick="cmd('tps')">📊 TPS Sorgula</button>
      </div>
    </div>
    <div class="card">
      <div class="card-hd">⚡ JVM Optimizasyonları</div>
      <div style="font-family:var(--mono);font-size:11px;color:var(--t2);line-height:1.8">
        G1GC · ParallelRefProc · MaxGCPause=200ms · CompressedClassSpace=32MB ·
        MaxMetaspace=128MB · G1NewSize=20% · G1MaxNew=35% · G1HeapRegion=4M
      </div>
    </div>
  </div>
  </div>
</div>
</div>
<div class="modal-bg" id="modal">
  <div class="modal">
    <h3 id="m-title">—</h3>
    <div id="m-body"></div>
    <div class="modal-btns">
      <button class="btn b-ghost" onclick="closeModal()">İptal</button>
      <button class="btn b-prim"  id="m-ok">Tamam</button>
    </div>
  </div>
</div>
<div class="notif-wrap" id="notif-wrap"></div>
<script>
const socket = io({ transports: ['websocket', 'polling'] });
let curPage='dashboard', curFile=null, curDir='', mcAddr='', srvRunning=false;

socket.on('connect',        ()   => notify('Panele bağlandı', 'ok'));
socket.on('disconnect',     ()   => notify('Bağlantı kesildi', 'err'));
socket.on('console_line',   data => addLine(data));
socket.on('console_history',lines=> { document.getElementById('con-out').innerHTML=''; lines.forEach(l=>addLine(l,false)); conBottom(); });
socket.on('server_status',  data => updateStatus(data));
socket.on('players_update', list => updatePlayers(list));
socket.on('stats_update',   data => updateStats(data));
socket.on('tunnel_update',  data => setTunnel(data));
socket.on('download_progress', d => notify(`⬇ İndiriliyor: %${d.pct}`, 'info'));

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => { const p=el.dataset.page; if(p) navTo(p,el); });
});
function navTo(page, el) {
  document.querySelectorAll('.page').forEach(p=>{p.classList.remove('active');p.style.display='';});
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const pg=document.getElementById('page-'+page); if(!pg) return;
  pg.classList.add('active');
  if(page==='console') pg.style.display='flex';
  const nav=el||document.querySelector(`.nav-item[data-page="${page}"]`);
  if(nav) nav.classList.add('active');
  const titles={dashboard:'📊 Dashboard',console:'💻 Konsol',players:'👥 Oyuncular',
    whitelist:'📋 Beyaz Liste',banlist:'🔨 Ban Listesi',plugins:'🔌 Pluginler',
    files:'📁 Dosya Yöneticisi',worlds:'🌍 Dünyalar',backups:'💾 Yedekler',
    settings:'⚙️ Ayarlar',perf:'📈 Performans'};
  document.getElementById('page-title').textContent=titles[page]||page;
  curPage=page;
  const loaders={players:refreshPlayers,whitelist:loadWhitelist,banlist:loadBanlist,
    plugins:loadPlugins,files:()=>fmLoad(curDir),worlds:loadWorlds,backups:loadBackups,
    settings:loadSettings,perf:loadPerf};
  if(loaders[page]) loaders[page]();
}
function addLine(data,scroll=true) {
  const el=document.getElementById('con-out');
  const div=document.createElement('div');
  const l=data.line||'';
  let cls='cl-def';
  if(l.includes('[Panel]')) cls='cl-panel';
  else if(/error|exception/i.test(l)) cls='cl-err';
  else if(/warn/i.test(l)) cls='cl-warn';
  else if(l.includes('INFO')) cls='cl-info';
  div.className=cls; div.textContent=`[${data.ts}] ${l}`;
  el.appendChild(div);
  if(scroll) el.scrollTop=el.scrollHeight;
  const dl=document.getElementById('d-log');
  if(dl){const s=document.createElement('div');s.textContent=div.textContent;s.className=cls;
    dl.appendChild(s);while(dl.children.length>25)dl.removeChild(dl.firstChild);dl.scrollTop=dl.scrollHeight;}
}
function conClear(){document.getElementById('con-out').innerHTML='';}
function conBottom(){const e=document.getElementById('con-out');e.scrollTop=e.scrollHeight;}
function conSearch(){const q=prompt('Ara:');if(!q)return;document.querySelectorAll('#con-out div').forEach(d=>{d.style.background=d.textContent.toLowerCase().includes(q.toLowerCase())?'rgba(0,229,255,.15)':''});}
function conSend(){const inp=document.getElementById('con-inp');const c=inp.value.trim();if(!c)return;socket.emit('send_command',{cmd:c});inp.value='';}
document.addEventListener('DOMContentLoaded',()=>{document.getElementById('con-inp').addEventListener('keydown',e=>{if(e.key==='Enter')conSend();});});

function updateStatus(data) {
  srvRunning=data.status==='running';
  const map={stopped:['dot-red','Durduruldu','br'],starting:['dot-yellow','Başlıyor...','by'],
    downloading:['dot-yellow','İndiriliyor..','by'],running:['dot-green','Çalışıyor','bg'],stopping:['dot-yellow','Duruyor...','by']};
  const [dc,label,bc]=map[data.status]||map.stopped;
  document.getElementById('status-dot').className='dot '+dc;
  document.getElementById('status-text').textContent=label;
  const badge=document.getElementById('d-status');
  if(badge){badge.className='badge '+bc;badge.textContent=label;}
  const verEl=document.getElementById('sb-ver');
  if(verEl&&data.version&&data.version!=='—') verEl.textContent='Paper MC • '+data.version;
  const dvEl=document.getElementById('d-ver');
  if(dvEl) dvEl.textContent=data.version||'—';
}
function updateStats(data) {
  if(data.ram_mb!==undefined){document.getElementById('d-ram').textContent=data.ram_mb;document.getElementById('tb-ram').textContent=data.ram_mb+' MB';}
  if(data.tps!==undefined){document.getElementById('d-tps').textContent=data.tps;document.getElementById('tb-tps').textContent=data.tps;document.getElementById('d-tps3').textContent=`${data.tps} / ${data.tps5||'—'} / ${data.tps15||'—'}`;}
  if(data.uptime!==undefined) document.getElementById('d-up').textContent=fmtUp(data.uptime);
  if(data.online_players!==undefined){document.getElementById('d-pl').textContent=data.online_players;document.getElementById('tb-pl').textContent=data.online_players;}
}
async function srvAction(action){const r=await api('/api/'+action,{});notify(r.msg||action+' yapıldı',r.ok?'ok':'err');}
function setTunnel(data){
  if(!data.host&&!data.url)return;
  mcAddr=data.host||data.url.replace('https://','');
  const bar=document.getElementById('mc-addr-bar');
  const txt=document.getElementById('mc-addr-text');
  const daddr=document.getElementById('d-addr');
  if(bar) bar.classList.remove('hidden');
  if(txt) txt.textContent=mcAddr;
  if(daddr) daddr.textContent=mcAddr;
  notify('📌 MC Adres: '+mcAddr,'ok');
}
function copyAddr(){if(mcAddr){navigator.clipboard.writeText(mcAddr);notify('Adres kopyalandı!','ok');}}
function updatePlayers(list){
  document.getElementById('d-pl').textContent=list.length;
  document.getElementById('tb-pl').textContent=list.length;
  const plEl=document.getElementById('d-pllist');
  if(plEl) plEl.innerHTML=list.length?list.map(p=>`<div style="padding:3px 0">🟢 ${p.name}</div>`).join(''):'<span style="color:var(--t2)">Çevrimiçi oyuncu yok</span>';
  const tbody=document.getElementById('pl-body');
  if(!tbody)return;
  if(!list.length){tbody.innerHTML='<tr><td colspan="3" style="color:var(--t2);text-align:center;padding:14px">Çevrimiçi oyuncu yok</td></tr>';return;}
  tbody.innerHTML=list.map(p=>`<tr><td><strong>${p.name}</strong></td><td><span class="badge bg">Online</span></td>
    <td style="display:flex;gap:4px;flex-wrap:wrap">
      <button class="btn btn-sm b-dang" onclick="quickAct('kick','${p.name}')">Kick</button>
      <button class="btn btn-sm b-dang" onclick="quickAct('ban','${p.name}')">Ban</button>
      <button class="btn btn-sm b-succ" onclick="quickAct('op','${p.name}')">OP</button>
      <button class="btn btn-sm b-ghost" onclick="setGM('creative','${p.name}')">Creative</button>
      <button class="btn btn-sm b-ghost" onclick="quickAct('heal','${p.name}')">Heal</button>
    </td></tr>`).join('');
}
async function refreshPlayers(){const d=await api('/api/players');updatePlayers(d.players||[]);}
async function quickAct(action,player){await api('/api/players/'+action,{player});notify(`${player} → ${action}`,'ok');}
function plAct(action){const p=document.getElementById('pl-name').value.trim();if(!p)return notify('Oyuncu adı girin','err');quickAct(action,p);}
function setGM(mode,player){const p=player||document.getElementById('pl-name').value.trim();if(!p)return notify('Oyuncu adı girin','err');api('/api/players/gamemode',{player:p,mode});notify(`${p} → ${mode}`,'ok');}
function sendMsg(){const p=document.getElementById('msg-pl').value.trim();const m=document.getElementById('msg-txt').value.trim();if(!p||!m)return notify('Oyuncu ve mesaj gerekli','err');api('/api/players/msg',{player:p,message:m});notify('Mesaj gönderildi','ok');}
function giveItem(){const player=document.getElementById('pl-name').value.trim()||'@a';const item=document.getElementById('give-item').value.trim();const count=document.getElementById('give-count').value||1;if(!item)return notify('Item adı girin','err');api('/api/players/give',{player,item,count});notify(`Give: ${count}x ${item} → ${player}`,'ok');}
function broadcast(){const msg=prompt('Duyuru mesajı:');if(msg){cmd('say '+msg);notify('Duyuru gönderildi','ok');}}
async function loadWhitelist(){const d=await fetch('/api/whitelist').then(r=>r.json());const tb=document.getElementById('wl-body');tb.innerHTML=d.length?d.map(p=>`<tr><td>${p.name||p}</td><td style="font-family:var(--mono);font-size:10px;color:var(--t2)">${p.uuid||'—'}</td><td><button class="btn btn-sm b-dang" onclick="wlRemove('${p.name||p}')">Kaldır</button></td></tr>`).join(''):'<tr><td colspan="3" style="color:var(--t2);text-align:center;padding:12px">Beyaz liste boş</td></tr>';}
async function wlAdd(){const p=document.getElementById('wl-name').value.trim();if(!p)return;await api('/api/whitelist/add',{player:p});notify(p+' eklendi','ok');loadWhitelist();}
async function wlRemove(p){await api('/api/whitelist/remove',{player:p});notify(p+' kaldırıldı','ok');loadWhitelist();}
async function loadBanlist(){const d=await fetch('/api/banlist').then(r=>r.json());const tb=document.getElementById('ban-body');tb.innerHTML=d.length?d.map(p=>`<tr><td>${p.name}</td><td style="color:var(--t2);font-size:11px">${p.reason||'—'}</td><td style="font-size:10px;color:var(--t3)">${(p.created||'').slice(0,10)}</td><td><button class="btn btn-sm b-succ" onclick="pardon('${p.name}')">Affet</button></td></tr>`).join(''):'<tr><td colspan="4" style="color:var(--t2);text-align:center;padding:12px">Ban listesi boş</td></tr>';}
async function banPlayer(){const p=document.getElementById('ban-name').value.trim();const r=document.getElementById('ban-reason').value.trim()||'Banned by admin';if(!p)return;await api('/api/players/ban',{player:p,reason:r});notify(p+' banlandı','ok');loadBanlist();}
async function pardon(p){await api('/api/players/pardon',{player:p});notify(p+' affedildi','ok');loadBanlist();}
async function loadPlugins(){const d=await fetch('/api/plugins').then(r=>r.json());const tb=document.getElementById('plug-body');tb.innerHTML=d.length?d.map(p=>`<tr><td><strong>${p.name}</strong></td><td style="font-size:10px;color:var(--t2)">${fmtSize(p.size)}</td><td><span class="badge ${p.enabled?'bg':'br'}">${p.enabled?'Aktif':'Devre Dışı'}</span></td><td style="display:flex;gap:4px"><button class="btn btn-sm b-warn" onclick="togglePlugin('${p.file}')">${p.enabled?'Kapat':'Aç'}</button><button class="btn btn-sm b-dang" onclick="deletePlugin('${p.file}')">🗑</button></td></tr>`).join(''):'<tr><td colspan="4" style="color:var(--t2);text-align:center;padding:12px">Plugin yok.</td></tr>';}
async function uploadPlugin(input){const fd=new FormData();for(const f of input.files)fd.append(f.name,f);const r=await fetch('/api/plugins/upload',{method:'POST',body:fd});const d=await r.json();notify(d.msg||'Yüklendi','ok');loadPlugins();}
function dropPlugin(e){e.preventDefault();document.getElementById('plug-zone').classList.remove('drag');const fd=new FormData();for(const f of e.dataTransfer.files)if(f.name.endsWith('.jar'))fd.append(f.name,f);fetch('/api/plugins/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{notify(d.msg||'Yüklendi','ok');loadPlugins();});}
async function deletePlugin(file){if(!confirm(file+' silinsin mi?'))return;await api('/api/plugins/delete',{file});notify('Plugin silindi','ok');loadPlugins();}
async function togglePlugin(file){await api('/api/plugins/toggle',{file});loadPlugins();}
async function searchPlugins(){const q=document.getElementById('plug-q').value.trim();if(!q)return;const res=document.getElementById('plug-results');res.innerHTML='<div style="color:var(--t2);padding:10px">Aranıyor...</div>';const d=await fetch('/api/plugins/search?q='+encodeURIComponent(q)).then(r=>r.json());if(!d.length||d.error){res.innerHTML='<div style="color:var(--t2);padding:10px">Sonuç bulunamadı</div>';return;}res.innerHTML=d.map(p=>`<div style="display:flex;justify-content:space-between;align-items:flex-start;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.05)"><div><div style="font-size:13px;font-weight:600">${p.name}</div><div style="font-size:11px;color:var(--t2);margin-top:2px;max-width:320px">${p.description}</div><div style="font-size:10px;color:var(--t3);margin-top:2px">👤 ${p.owner} · ⬇ ${(p.downloads||0).toLocaleString()}</div></div><a class="btn btn-sm b-prim" href="${p.url}" target="_blank">🔗 Aç</a></div>`).join('');}
async function fmLoad(path=''){curDir=path;document.getElementById('fm-bread').textContent='/'+path;const items=await fetch('/api/files?path='+encodeURIComponent(path)).then(r=>r.json());const el=document.getElementById('fm-list');el.innerHTML=items.map(f=>`<div class="fm-item" onclick="fmClick('${f.path}','${f.type}','${escQ(f.name)}')"><span class="fm-ico">${f.type==='dir'?'📁':fmIco(f.ext)}</span><span class="fm-name">${f.name}</span><span class="fm-size">${f.type==='dir'?'':fmtSize(f.size)}</span></div>`).join('')||'<div style="padding:12px;color:var(--t2);font-size:12px">Klasör boş</div>';}
function escQ(s){return s.replace(/'/g,"\\'");}
function fmIco(ext){const m={'.properties':'⚙️','.json':'📋','.yml':'📋','.yaml':'📋','.jar':'☕','.txt':'📄','.log':'📜','.sh':'🖥️','.zip':'📦','.png':'🖼️','.jpg':'🖼️','.dat':'🗃️','.conf':'⚙️','.toml':'⚙️'};return m[ext]||'📄';}
function fmUp(){const parts=curDir.split('/').filter(Boolean);parts.pop();fmLoad(parts.join('/'));}
function fmRefresh(){fmLoad(curDir);}
async function fmClick(path,type,name){document.querySelectorAll('.fm-item').forEach(i=>i.classList.remove('sel'));event.currentTarget.classList.add('sel');curFile=path;if(type==='dir'){fmLoad(path);return;}document.getElementById('fm-fname').textContent=name;const textExts=['.properties','.json','.yml','.yaml','.txt','.log','.sh','.conf','.toml','.cfg','.xml','.md','.java','.py','.js'];const ext='.'+name.split('.').pop().toLowerCase();if(textExts.includes(ext)){const d=await fetch('/api/files/read?path='+encodeURIComponent(path)).then(r=>r.json());document.getElementById('fm-area').value=d.content||'';document.getElementById('fm-save').disabled=false;}else{document.getElementById('fm-area').value='(Bu dosya türü metin editöründe açılamaz)';document.getElementById('fm-save').disabled=true;}}
async function fmSave(){if(!curFile)return;const content=document.getElementById('fm-area').value;await api('/api/files/write',{path:curFile,content});notify('Kaydedildi','ok');document.getElementById('fm-save').disabled=true;}
function fmDownload(){if(curFile)window.open('/api/files/download?path='+encodeURIComponent(curFile));}
async function fmDelete(){if(!curFile||!confirm(curFile+' silinsin mi?'))return;await api('/api/files/delete',{path:curFile});notify('Silindi','ok');fmLoad(curDir);curFile=null;document.getElementById('fm-area').value='';document.getElementById('fm-fname').textContent='Dosya seçin...';}
async function fmUpload(input){const fd=new FormData();fd.append('path',curDir);for(const f of input.files)fd.append(f.name,f);await fetch('/api/files/upload',{method:'POST',body:fd});notify('Yüklendi','ok');fmLoad(curDir);}
function fmNewModal(){showModal('Yeni Dosya / Klasör','<input class="m-inp" id="nf-n" placeholder="isim.txt veya klasör-adı">',async()=>{const n=document.getElementById('nf-n').value.trim();if(!n)return;if(n.includes('.'))await api('/api/files/write',{path:curDir+'/'+n,content:''});else await api('/api/files/mkdir',{path:curDir+'/'+n});fmLoad(curDir);closeModal();});}
async function loadWorlds(){const d=await fetch('/api/worlds').then(r=>r.json());const el=document.getElementById('worlds-list');el.innerHTML=d.length?d.map(w=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.05)"><div><div style="font-weight:600">🌍 ${w.name}</div><div style="font-size:11px;color:var(--t2);margin-top:2px">Boyut: ${fmtSize(w.size)}</div></div><div style="display:flex;gap:8px"><button class="btn btn-sm b-prim" onclick="backupWorld('${w.name}')">💾 Yedekle</button><button class="btn btn-sm b-dang" onclick="deleteWorld('${w.name}')">🗑 Sil</button></div></div>`).join(''):'<div style="color:var(--t2)">Dünya bulunamadı</div>';}
async function backupWorld(name){notify('Yedekleniyor...','info');const d=await api('/api/worlds/backup',{world:name});notify(d.ok?`Yedeklendi: ${d.file} (${fmtSize(d.size)})`:'Hata',d.ok?'ok':'err');loadBackups();}
async function deleteWorld(name){if(!confirm(name+' dünyası kalıcı olarak silinsin mi?'))return;const d=await api('/api/worlds/delete',{world:name});notify(d.ok?'Dünya silindi':d.error,d.ok?'ok':'err');loadWorlds();}
async function loadBackups(){const d=await fetch('/api/backups').then(r=>r.json());const tb=document.getElementById('backup-body');tb.innerHTML=d.length?d.map(b=>`<tr><td>${b.name}</td><td style="font-size:11px;color:var(--t2)">${fmtSize(b.size)}</td><td style="font-size:11px;color:var(--t3)">${new Date(b.created*1000).toLocaleString('tr')}</td><td><a class="btn btn-sm b-prim" href="/api/files/download?path=${encodeURIComponent(b.path)}">⬇ İndir</a></td></tr>`).join(''):'<tr><td colspan="4" style="color:var(--t2);text-align:center;padding:12px">Yedek yok</td></tr>';}
const SET_LABELS={'server-port':'Sunucu Portu','max-players':'Max Oyuncu','online-mode':'Online Mode','gamemode':'Oyun Modu','difficulty':'Zorluk','motd':'Sunucu Adı (MOTD)','view-distance':'Görüş Mesafesi','simulation-distance':'Simülasyon Mesafesi','spawn-protection':'Spawn Koruması','allow-flight':'Uçuşa İzin','white-list':'Beyaz Liste','enable-command-block':'Komut Bloğu','pvp':'PvP','allow-nether':'Nether Boyutu','level-name':'Dünya Adı','level-seed':'Dünya Seed','max-tick-time':'Max Tick Süresi (ms)','sync-chunk-writes':'Chunk Sync Yazma','generate-structures':'Yapı Oluştur'};
async function loadSettings(){const d=await fetch('/api/settings').then(r=>r.json());const el=document.getElementById('settings-grid');el.innerHTML=Object.entries(d).map(([k,v])=>`<div class="set-item"><div class="set-lbl">${SET_LABELS[k]||k}</div>${v==='true'||v==='false'?`<select class="set-inp" id="s-${k}"><option value="true" ${v==='true'?'selected':''}>true</option><option value="false" ${v==='false'?'selected':''}>false</option></select>`:`<input class="set-inp" id="s-${k}" value="${v.replace(/</g,'&lt;')}">`}</div>`).join('');}
async function saveSettings(){const d=await fetch('/api/settings').then(r=>r.json());const updated={};for(const k of Object.keys(d)){const el=document.getElementById('s-'+k);if(el)updated[k]=el.value;}const r=await api('/api/settings',updated);notify(r.msg||'Kaydedildi','ok');setTimeout(()=>srvAction('restart'),1500);}
async function loadPerf(){const d=await fetch('/api/performance').then(r=>r.json());if(d.cpu!==undefined){document.getElementById('p-cpu').textContent=d.cpu+'%';document.getElementById('pb-cpu').style.width=d.cpu+'%';}if(d.ram_pct!==undefined){document.getElementById('p-ram').textContent=`${d.ram_used_mb}MB / ${d.ram_total_mb}MB`;document.getElementById('pb-ram').style.width=d.ram_pct+'%';}if(d.disk_pct!==undefined){document.getElementById('p-disk').textContent=`${d.disk_used_gb}GB / ${d.disk_total_gb}GB`;document.getElementById('pb-disk').style.width=d.disk_pct+'%';}if(d.mc&&d.mc.ram){document.getElementById('p-mcram').textContent=d.mc.ram+' MB';document.getElementById('p-mccpu').textContent=d.mc.cpu+'%';document.getElementById('p-mcthr').textContent=d.mc.threads;}document.getElementById('p-tps1').textContent=d.tps||'—';document.getElementById('p-tps5').textContent=d.tps5||'—';document.getElementById('p-tps15').textContent=d.tps15||'—';document.getElementById('p-pls').textContent=document.getElementById('tb-pl').textContent;}
setInterval(()=>{if(curPage==='perf')loadPerf();},5000);
function cmd(c){socket.emit('send_command',{cmd:c});notify('→ '+c,'info');}
async function api(url,body={}){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return r.json();}
function fmtSize(b){if(b>1e9)return(b/1e9).toFixed(2)+'GB';if(b>1e6)return(b/1e6).toFixed(1)+'MB';if(b>1e3)return(b/1e3).toFixed(0)+'KB';return b+'B';}
function fmtUp(s){const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;return `${h}s ${m}d ${sec}sn`;}
function notify(msg,type='ok'){const wrap=document.getElementById('notif-wrap');const div=document.createElement('div');div.className='notif n-'+type;div.textContent=msg;wrap.appendChild(div);setTimeout(()=>div.remove(),3500);}
function showModal(title,body,onok){document.getElementById('m-title').textContent=title;document.getElementById('m-body').innerHTML=body;document.getElementById('m-ok').onclick=onok;document.getElementById('modal').classList.add('open');setTimeout(()=>{const inp=document.querySelector('#m-body input');if(inp)inp.focus();},50);}
function closeModal(){document.getElementById('modal').classList.remove('open');}
document.getElementById('modal').addEventListener('click',e=>{if(e.target===document.getElementById('modal'))closeModal();});
async function init(){const d=await fetch('/api/status').then(r=>r.json()).catch(()=>({}));updateStatus(d);updateStats(d);if(d.players)updatePlayers(d.players);if(d.tunnel&&d.tunnel.host)setTunnel(d.tunnel);}
init();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════

threading.Thread(target=_ram_monitor,   daemon=True).start()
threading.Thread(target=_ram_watchdog,  daemon=True).start()

if __name__ == "__main__":
    MC_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[MC Panel] :{PANEL_PORT} başlatılıyor...")
    socketio.run(app, host="0.0.0.0", port=PANEL_PORT,
                 debug=False, use_reloader=False, log_output=False)
