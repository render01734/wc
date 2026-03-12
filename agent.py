"""
⚙️  RESOURCE AGENT v1.0  —  Destek Sunucusu
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Kernel modülü gerektirmez. Saf Python/userspace.

Sağlanan kaynaklar:
  1. RAM Cache    → Minecraft chunk/entity verilerini bellekte tutar
  2. File Store   → Dünya region dosyalarını disk'te saklar (HTTP API)
  3. TCP Proxy    → Oyuncu bağlantılarını ana sunucuya iletir
  4. CPU Worker   → Chunk ön üretimi, sıkıştırma görevleri

Ana sunucu bu agent'leri otomatik keşfeder (cloudflare tunnel URL kaydı).
"""

import os, sys, time, json, threading, subprocess, re, hashlib
import struct, socket, ssl, resource, glob, shutil, gzip, io
from pathlib import Path
from collections import OrderedDict
import urllib.request as _ur
import urllib.parse as _up

from flask import Flask, request, jsonify, send_file, Response, abort

# ─────────────────────────────────────────────
PORT         = int(os.environ.get("PORT", "5000"))
MAIN_URL     = os.environ.get("MAIN_URL", "https://wc-tsgd.onrender.com")

# DÜZELTİLDİ: NODE_ID artık kararlı (her restart'ta aynı kalır).
# Öncelik: NODE_ID env → RENDER_EXTERNAL_URL → URL hash (PID YOK)
# Eski kod os.getpid() kullanıyordu → her restart'ta farklı ID → ana sunucu
# aynı agent'ı yeni node sanıyordu.
_render_url  = os.environ.get("RENDER_EXTERNAL_URL", "")
_url_slug    = _render_url.replace("https://", "").replace(".onrender.com", "")
NODE_ID      = (
    os.environ.get("NODE_ID")          # Manuel override
    or _url_slug                       # Render URL'den slug (kararlı)
    or ("agent-" + hashlib.md5(_render_url.encode() or b"local").hexdigest()[:8])
)

DATA_DIR     = Path("/agent_data")          # Dosya deposu
# Agent 382MB free RAM'e sahip. Cache için 350MB ayır (32MB Flask/OS büyümesi için bırak).
# Ana sunucu bu limiti agent'ın /api/cache/stats endpoint'inden okur.
RAM_CACHE_MB  = int(os.environ.get("RAM_CACHE_MB",   "350"))  # RAM önbelleği boyutu
RAM_LIMIT_MB  = int(os.environ.get("RAM_LIMIT_MB",   "512"))  # Render plan RAM kotası
DISK_LIMIT_GB = float(os.environ.get("DISK_LIMIT_GB", "17.5")) # Render plan Disk kotası
AGENT_OVERHEAD_MB = 130  # Flask + psutil + OS taban kullanımı (~130MB)
# ─────────────────────────────────────────────

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "regions").mkdir(exist_ok=True)
(DATA_DIR / "chunks").mkdir(exist_ok=True)
(DATA_DIR / "backups").mkdir(exist_ok=True)

app   = Flask(__name__)
state = {
    "node_id":   NODE_ID,
    "tunnel":    "",
    "ram_cache": {"used_mb": 0, "limit_mb": RAM_CACHE_MB, "keys": 0},
    "file_store":{"used_gb": 0.0, "files": 0},
    "proxy":     {"active": False, "port": 0, "connections": 0},
    "cpu_queue": [],
}

# ══════════════════════════════════════════════════════════════
#  1. RAM CACHE  (LRU, sıkıştırılmış)
# ══════════════════════════════════════════════════════════════

class RamCache:
    """
    LRU RAM önbelleği.
    Veriyi gzip ile sıkıştırarak bellekte saklar.
    Limit aşılınca en eski anahtarları atar.
    """
    def __init__(self, limit_mb: int):
        self.limit   = limit_mb * 1024 * 1024
        self.store   = OrderedDict()   # key → compressed_bytes
        self.size    = 0
        self.lock    = threading.Lock()
        self.hits    = 0
        self.misses  = 0

    def set(self, key: str, data: bytes) -> bool:
        compressed = gzip.compress(data, compresslevel=1)
        sz = len(compressed)
        with self.lock:
            if key in self.store:
                self.size -= len(self.store[key])
                del self.store[key]
            while self.size + sz > self.limit and self.store:
                _, old = self.store.popitem(last=False)
                self.size -= len(old)
            if sz > self.limit:
                return False
            self.store[key] = compressed
            self.store.move_to_end(key)
            self.size += sz
        self._update_state()
        return True

    def get(self, key: str):
        with self.lock:
            if key not in self.store:
                self.misses += 1
                return None
            self.store.move_to_end(key)
            compressed = self.store[key]
            self.hits += 1
        return gzip.decompress(compressed)

    def delete(self, key: str) -> bool:
        with self.lock:
            if key not in self.store:
                return False
            self.size -= len(self.store.pop(key))
        self._update_state()
        return True

    def keys_with_prefix(self, prefix: str) -> list:
        with self.lock:
            return [k for k in self.store if k.startswith(prefix)]

    def _update_state(self):
        with self.lock:
            state["ram_cache"]["used_mb"]  = self.size // 1024 // 1024
            state["ram_cache"]["keys"]     = len(self.store)

    @property
    def stats(self):
        with self.lock:
            total = self.hits + self.misses
            return {
                "used_mb":   self.size // 1024 // 1024,
                "limit_mb":  self.limit // 1024 // 1024,
                "keys":      len(self.store),
                "hits":      self.hits,
                "misses":    self.misses,
                "hit_rate":  round(self.hits / total * 100, 1) if total else 0,
            }


ram_cache = RamCache(RAM_CACHE_MB)


@app.route("/api/cache/set", methods=["POST"])
def cache_set():
    key  = request.args.get("key", "")
    data = request.get_data()
    if not key or not data:
        return jsonify({"ok": False, "error": "key veya data eksik"}), 400
    ok = ram_cache.set(key, data)
    return jsonify({"ok": ok, "size": len(data), "compressed": ok})


@app.route("/api/cache/get/<path:key>")
def cache_get(key):
    data = ram_cache.get(key)
    if data is None:
        return jsonify({"ok": False, "error": "miss"}), 404
    return Response(data, mimetype="application/octet-stream")


@app.route("/api/cache/delete/<path:key>", methods=["DELETE", "POST"])
def cache_delete(key):
    ok = ram_cache.delete(key)
    return jsonify({"ok": ok})


@app.route("/api/cache/keys")
def cache_keys():
    prefix = request.args.get("prefix", "")
    keys = ram_cache.keys_with_prefix(prefix) if prefix else list(ram_cache.store.keys())
    return jsonify({"keys": keys, "count": len(keys)})


@app.route("/api/cache/stats")
def cache_stats():
    return jsonify(ram_cache.stats)


@app.route("/api/cache/flush", methods=["POST"])
def cache_flush():
    prefix = (request.json or {}).get("prefix", "")
    with ram_cache.lock:
        if prefix:
            to_del = [k for k in list(ram_cache.store) if k.startswith(prefix)]
            for k in to_del:
                ram_cache.size -= len(ram_cache.store.pop(k))
            count = len(to_del)
        else:
            count = len(ram_cache.store)
            ram_cache.store.clear()
            ram_cache.size = 0
    ram_cache._update_state()
    return jsonify({"ok": True, "flushed": count})


# ══════════════════════════════════════════════════════════════
#  2. FILE STORE  (HTTP — bölge dosyaları, yedekler)
# ══════════════════════════════════════════════════════════════

def _safe_path(category: str, filename: str) -> Path:
    allowed = {"regions", "chunks", "backups", "plugins", "configs"}
    if category not in allowed:
        abort(403)
    p = (DATA_DIR / category / filename).resolve()
    if not str(p).startswith(str((DATA_DIR / category).resolve())):
        abort(403)
    return p


def _disk_used_gb() -> float:
    total = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
    state["file_store"]["used_gb"] = round(total / 1e9, 2)
    state["file_store"]["files"]   = sum(1 for _ in DATA_DIR.rglob("*") if _.is_file())
    return total / 1e9


@app.route("/api/files/<category>", methods=["GET"])
def file_list(category):
    d = DATA_DIR / category
    d.mkdir(exist_ok=True)
    files = []
    for f in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({
                "name":     f.name,
                "size":     f.stat().st_size,
                "modified": int(f.stat().st_mtime),
                "md5":      None,
            })
    return jsonify({"files": files, "count": len(files)})


@app.route("/api/files/<category>/<path:filename>", methods=["PUT", "POST"])
def file_upload(category, filename):
    p = _safe_path(category, filename)
    if _disk_used_gb() > DISK_LIMIT_GB - 0.5:
        return jsonify({"ok": False, "error": "Disk dolu"}), 507
    p.parent.mkdir(parents=True, exist_ok=True)
    data = request.get_data()
    p.write_bytes(data)
    return jsonify({"ok": True, "size": len(data), "path": str(p.relative_to(DATA_DIR))})


@app.route("/api/files/<category>/<path:filename>", methods=["GET"])
def file_download(category, filename):
    p = _safe_path(category, filename)
    if not p.exists():
        return jsonify({"ok": False, "error": "Dosya bulunamadı"}), 404
    return send_file(str(p), as_attachment=True, download_name=p.name)


@app.route("/api/files/<category>/<path:filename>", methods=["DELETE"])
def file_delete(category, filename):
    p = _safe_path(category, filename)
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


@app.route("/api/files/<category>/<path:filename>/exists")
def file_exists(category, filename):
    p = _safe_path(category, filename)
    exists = p.exists()
    size   = p.stat().st_size if exists else 0
    return jsonify({"exists": exists, "size": size})


@app.route("/api/files/storage/stats")
def storage_stats():
    used = _disk_used_gb()
    cats = {}
    for cat in ["regions", "chunks", "backups", "plugins", "configs"]:
        d = DATA_DIR / cat
        if d.exists():
            s = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            cats[cat] = {"files": sum(1 for _ in d.rglob("*") if _.is_file()), "size_mb": round(s/1e6, 1)}
    return jsonify({
        "used_gb":  round(used, 2),
        "limit_gb": DISK_LIMIT_GB,
        "free_gb":  round(DISK_LIMIT_GB - used, 2),
        "categories": cats,
    })


# ══════════════════════════════════════════════════════════════
#  3. TCP PROXY  (oyuncu bağlantı iletici)
# ══════════════════════════════════════════════════════════════

_proxy_state = {
    "active":      False,
    "listen_port": 0,
    "target_host": "",
    "target_port": 25565,
    "connections": 0,
    "bytes_fwd":   0,
}
_proxy_stop = threading.Event()


def _relay(src, dst, label, byte_counter):
    try:
        while True:
            data = src.recv(32768)
            if not data:
                break
            dst.sendall(data)
            byte_counter[0] += len(data)
    except Exception:
        pass
    finally:
        try: src.close()
        except: pass
        try: dst.close()
        except: pass


def _handle_proxy_conn(client_sock):
    host = _proxy_state["target_host"]
    port = _proxy_state["target_port"]
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.settimeout(10)
        srv.connect((host, port))
        srv.settimeout(None)
    except Exception:
        client_sock.close()
        return
    _proxy_state["connections"] += 1
    bc = [0]
    t1 = threading.Thread(target=_relay, args=(client_sock, srv,   "c→s", bc), daemon=True)
    t2 = threading.Thread(target=_relay, args=(srv,   client_sock, "s→c", bc), daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()
    _proxy_state["connections"] -= 1
    _proxy_state["bytes_fwd"] += bc[0]


def _proxy_server_loop(listen_port, target_host, target_port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(32)
    srv.settimeout(1.0)
    _proxy_state.update({"active": True, "listen_port": listen_port,
                          "target_host": target_host, "target_port": target_port})
    state["proxy"]["active"] = True
    state["proxy"]["port"]   = listen_port
    print(f"  [proxy] ✅ TCP relay :{listen_port} → {target_host}:{target_port}")
    while not _proxy_stop.is_set():
        try:
            conn, _ = srv.accept()
            threading.Thread(target=_handle_proxy_conn, args=(conn,), daemon=True).start()
        except socket.timeout:
            continue
        except Exception:
            break
    srv.close()
    _proxy_state["active"] = False


@app.route("/api/proxy/start", methods=["POST"])
def proxy_start():
    d = request.json or {}
    target_host = d.get("host", "")
    target_port = int(d.get("port", 25565))
    listen_port = int(d.get("listen_port", 25565))
    if not target_host:
        return jsonify({"ok": False, "error": "host gerekli"})
    if _proxy_state["active"]:
        return jsonify({"ok": False, "error": "Proxy zaten aktif"})
    _proxy_stop.clear()
    t = threading.Thread(target=_proxy_server_loop,
                         args=(listen_port, target_host, target_port), daemon=True)
    t.start()
    return jsonify({"ok": True, "listen_port": listen_port,
                    "target": f"{target_host}:{target_port}"})


@app.route("/api/proxy/stop", methods=["POST"])
def proxy_stop():
    _proxy_stop.set()
    _proxy_state["active"] = False
    return jsonify({"ok": True})


@app.route("/api/proxy/status")
def proxy_status():
    return jsonify({**_proxy_state,
                    "bytes_fwd_mb": round(_proxy_state["bytes_fwd"] / 1e6, 2)})


# ══════════════════════════════════════════════════════════════
#  4. CPU WORKER  (görev kuyruğu)
# ══════════════════════════════════════════════════════════════

_task_queue   = []
_task_results = {}
_task_lock    = threading.Lock()
_task_counter = 0

ALLOWED_COMMANDS = {
    "du", "df", "ls", "find", "wc", "md5sum", "sha256sum",
    "gzip", "gunzip", "tar", "zip", "unzip",
}


def _run_task(task_id: str, task: dict):
    t_type  = task.get("type", "")
    payload = task.get("payload", {})
    result  = {"ok": False, "error": "Bilinmeyen görev türü"}

    try:
        if t_type == "compress_file":
            src  = DATA_DIR / payload["path"]
            dst  = DATA_DIR / (payload.get("dest") or payload["path"] + ".gz")
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(src, "rb") as f_in, gzip.open(dst, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out)
            result = {"ok": True, "src": str(src), "dst": str(dst), "size": dst.stat().st_size}

        elif t_type == "decompress_file":
            src = DATA_DIR / payload["path"]
            dst = DATA_DIR / payload.get("dest", str(payload["path"]).replace(".gz", ""))
            dst.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(src, "rb") as f_in, open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            result = {"ok": True, "dst": str(dst), "size": dst.stat().st_size}

        elif t_type == "hash_files":
            directory = DATA_DIR / payload.get("dir", ".")
            hashes = {}
            for f in sorted(directory.rglob("*")):
                if f.is_file():
                    md5 = hashlib.md5(f.read_bytes()).hexdigest()
                    hashes[str(f.relative_to(DATA_DIR))] = md5
            result = {"ok": True, "hashes": hashes, "count": len(hashes)}

        elif t_type == "run_command":
            # DÜZELTİLDİ: shell=True + whitelist birlikte güvensizdi.
            # Artık whitelist geçtikten sonra da shell=False ile çalıştırılıyor.
            cmd_str   = payload.get("cmd", "")
            cmd_parts = cmd_str.split()
            if not cmd_parts or cmd_parts[0] not in ALLOWED_COMMANDS:
                result = {"ok": False, "error": f"Komut izin verilmiyor: {cmd_parts[0] if cmd_parts else '?'}"}
            else:
                r = subprocess.run(cmd_parts, capture_output=True,
                                   timeout=30, cwd=str(DATA_DIR))
                result = {
                    "ok":     r.returncode == 0,
                    "stdout": r.stdout.decode()[:4096],
                    "stderr": r.stderr.decode()[:1024],
                    "code":   r.returncode,
                }

        elif t_type == "chunk_stats":
            region_dir = DATA_DIR / "regions" / payload.get("dimension", "overworld")
            stats = {"regions": 0, "total_size_mb": 0}
            if region_dir.exists():
                files = list(region_dir.glob("*.mca"))
                stats["regions"]        = len(files)
                stats["total_size_mb"]  = round(sum(f.stat().st_size for f in files) / 1e6, 1)
                stats["largest_region"] = max((f.name for f in files),
                                               key=lambda n: (region_dir / n).stat().st_size,
                                               default=None)
            result = {"ok": True, **stats}

        elif t_type == "echo":
            result = {"ok": True, "echo": payload}

    except Exception as e:
        result = {"ok": False, "error": str(e)}

    with _task_lock:
        _task_results[task_id] = result
        state["cpu_queue"] = [t for t in _task_queue if t["id"] not in _task_results]


@app.route("/api/cpu/submit", methods=["POST"])
def cpu_submit():
    global _task_counter
    task = request.json or {}
    if not task.get("type"):
        return jsonify({"ok": False, "error": "type gerekli"})
    with _task_lock:
        _task_counter += 1
        task_id = f"task_{_task_counter}_{int(time.time())}"
        task["id"] = task_id
        _task_queue.append(task)
    threading.Thread(target=_run_task, args=(task_id, task), daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/cpu/result/<task_id>")
def cpu_result(task_id):
    with _task_lock:
        r = _task_results.get(task_id)
    if r is None:
        return jsonify({"ok": False, "status": "pending"})
    return jsonify({"ok": True, "status": "done", "result": r})


@app.route("/api/cpu/queue")
def cpu_queue():
    with _task_lock:
        pending   = [t["id"] for t in _task_queue if t["id"] not in _task_results]
        completed = list(_task_results.keys())
    return jsonify({"pending": pending, "completed": completed[-20:]})


# ══════════════════════════════════════════════════════════════
#  5. KAYNAK BİLDİRGE  (heartbeat + register)
# ══════════════════════════════════════════════════════════════

def _get_resource_info() -> dict:
    import psutil
    cpu  = psutil.cpu_count(logical=True)
    try:
        load1, load5, _ = os.getloadavg()
    except:
        load1 = load5 = 0.0

    # ── Render sınırlı RAM ──────────────────────────────────
    # psutil.virtual_memory() host RAM'ini okur (10-30GB) — yanıltıcı.
    # Kendi process RSS + ram_cache kullanımı üzerinden hesapla.
    try:
        my_rss_mb = int(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)
    except:
        my_rss_mb = AGENT_OVERHEAD_MB
    cache_mb     = ram_cache.stats["used_mb"]
    agent_used_mb = max(my_rss_mb, AGENT_OVERHEAD_MB)  # en az overhead kadar say
    ram_free_mb  = max(0, RAM_LIMIT_MB - agent_used_mb)

    # ── Render sınırlı Disk ─────────────────────────────────
    # Sadece /agent_data kullanımını ölç — host FS değil.
    disk_store_gb = round(_disk_used_gb(), 2)
    disk_free_gb  = round(max(0.0, DISK_LIMIT_GB - disk_store_gb), 2)

    return {
        "node_id":      NODE_ID,
        "tunnel":       state["tunnel"],
        "ram": {
            "total_mb":   RAM_LIMIT_MB,
            "free_mb":    ram_free_mb,
            "used_mb":    agent_used_mb,
            "cache_mb":   cache_mb,
            "cache_keys": ram_cache.stats["keys"],
        },
        "disk": {
            "total_gb": DISK_LIMIT_GB,
            "free_gb":  disk_free_gb,
            "store_gb": disk_store_gb,
        },
        "cpu": {
            "cores":  cpu,
            "load1":  round(load1, 2),
            "load5":  round(load5, 2),
        },
        "proxy":   _proxy_state.copy(),
        "uptime":  int(time.time() - _start_time),
        "version": "agent-1.0",
    }


_start_time = time.time()


def _keepalive_loop():
    """Her 10 dakikada self+ana sunucu ping — Render free tier uyutma."""
    while True:
        time.sleep(600)
        try: _ur.urlopen(f"http://localhost:{PORT}/ping", timeout=5)
        except: pass
        try: _ur.urlopen(f"{MAIN_URL.rstrip('/')}/api/ping", timeout=10)
        except: pass


def _register_loop():
    """Ana sunucuya kayıt ol, heartbeat gönder."""
    # Tünel hazır olana kadar bekle
    while not state["tunnel"]:
        time.sleep(2)

    print(f"  [agent] ✅ Tünel hazır, ana sunucuya kayıt başlıyor...")
    attempt = 0
    while True:
        attempt += 1
        try:
            info = _get_resource_info()
            data = json.dumps(info).encode()
            _ur.urlopen(
                _ur.Request(
                    f"{MAIN_URL.rstrip('/')}/api/agent/register",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=15,
            )
            print(f"  [agent] ✅ Kayıt başarılı #{attempt} | RAM:{info['ram']['free_mb']}MB boş | Disk:{info['disk']['free_gb']}GB boş")
            break
        except Exception as e:
            print(f"  [agent] ⚠️  Kayıt #{attempt}: {e} — 30sn sonra...")
            time.sleep(30)

    # Heartbeat döngüsü — fail sayacı + otomatik re-register
    _hb_fail = 0
    while True:
        time.sleep(20)
        try:
            info = _get_resource_info()
            if not info.get("tunnel"):
                continue
            _ur.urlopen(
                _ur.Request(
                    f"{MAIN_URL.rstrip('/')}/api/agent/heartbeat",
                    data=json.dumps(info).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=10,
            )
            _hb_fail = 0  # başarı → sıfırla
        except Exception:
            _hb_fail += 1
            # 3 ardışık hata = ana sunucu uyumuş/restart olmuş → yeniden kayıt yap
            if _hb_fail >= 3:
                print(f"  [agent] ⚠️  {_hb_fail} heartbeat hatası → yeniden kayıt deneniyor...")
                for _attempt in range(8):
                    try:
                        _info2 = _get_resource_info()
                        if not _info2.get("tunnel"):
                            time.sleep(10); continue
                        _ur.urlopen(
                            _ur.Request(
                                f"{MAIN_URL.rstrip('/')}/api/agent/register",
                                data=json.dumps(_info2).encode(),
                                headers={"Content-Type": "application/json"},
                                method="POST",
                            ),
                            timeout=15,
                        )
                        print(f"  [agent] ✅ Yeniden kayıt başarılı (deneme {_attempt+1})")
                        _hb_fail = 0
                        break
                    except Exception as _e2:
                        time.sleep(15)


# ══════════════════════════════════════════════════════════════
#  CLOUDFLARE TÜNELİ
# ══════════════════════════════════════════════════════════════

def _start_tunnel():
    log = "/tmp/cf_agent.log"
    print("  [agent] 🌐 Cloudflare HTTP tüneli başlatılıyor...")
    subprocess.Popen(
        ["cloudflared", "tunnel",
         "--url", f"http://localhost:{PORT}",
         "--no-autoupdate", "--loglevel", "info"],
        stdout=open(log, "w"), stderr=subprocess.STDOUT,
    )
    for _ in range(240):
        try:
            content = open(log).read()
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
            if urls:
                url  = urls[0]
                host = url.replace("https://", "")
                state["tunnel"] = url
                print(f"\n  [agent] ✅ Tünel: {host}\n")
                return url
        except:
            pass
        time.sleep(0.5)
    print("  [agent] ⚠️  Tünel URL alınamadı (120sn)")
    return ""


# ══════════════════════════════════════════════════════════════
#  SAĞLIK + PANEL SAYFASI
# ══════════════════════════════════════════════════════════════

@app.route("/ping")
def quick_ping():
    """Render keep-alive — hafif, hızlı."""
    return {"ok": True, "node": NODE_ID, "t": int(time.time())}, 200


@app.route("/")
@app.route("/health")
def health():
    info = _get_resource_info()
    tunnel = state["tunnel"].replace("https://","") or "bekleniyor..."
    ram_pct  = min(100, int(info["ram"]["cache_mb"] / max(1, RAM_CACHE_MB) * 100))
    disk_pct = min(100, int(info["disk"]["store_gb"] / DISK_LIMIT_GB * 100))
    rc = "#ff4757" if ram_pct  > 85 else "#00e5ff"
    dc = "#ff4757" if disk_pct > 85 else "#00e5ff"
    return f"""<!DOCTYPE html><html lang="tr"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="8">
<title>Resource Agent v1.0</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#05060c;color:#eef0f8;font-family:'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#0f1120;border:1px solid rgba(0,229,255,.2);border-radius:16px;padding:28px 32px;max-width:560px;width:92%}}
h1{{font-size:18px;font-weight:700;color:#00e5ff;margin-bottom:4px}}
.sub{{font-size:11px;color:#8892a4;margin-bottom:18px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}}
.s{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:13px 12px}}
.sv{{font-size:20px;font-weight:700;font-family:monospace}}
.sl{{font-size:10px;color:#8892a4;margin-top:3px}}
.bw{{background:rgba(255,255,255,.06);border-radius:3px;height:4px;margin-top:6px;overflow:hidden}}
.b{{height:100%;border-radius:3px}}
.badge{{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:10px;font-weight:700;background:rgba(0,229,255,.1);border:1px solid rgba(0,229,255,.25);color:#00e5ff;margin-bottom:14px}}
.dot{{width:7px;height:7px;border-radius:50%;background:#00e5ff;box-shadow:0 0 5px #00e5ff;animation:blink 1.5s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.services{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}}
.svc{{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px 6px;text-align:center;font-size:11px}}
.svc-ico{{font-size:20px;margin-bottom:4px}}
.svc-lbl{{color:#8892a4;font-size:10px}}
.svc-val{{font-family:monospace;color:#00e5ff;margin-top:2px}}
.link{{display:inline-block;margin-top:8px;padding:8px 20px;background:linear-gradient(135deg,#00e5ff,#7c6aff);color:#000;border-radius:8px;font-weight:700;text-decoration:none;font-size:12px}}
.tun{{font-family:monospace;font-size:11px;color:#7c6aff;background:rgba(124,106,255,.1);border-radius:6px;padding:6px 10px;margin-bottom:12px;word-break:break-all}}
</style></head><body><div class="card">
<div style="font-size:36px;margin-bottom:8px">⚙️</div>
<div class="badge"><div class="dot"></div> RESOURCE AGENT v1.0 — AKTİF</div>
<h1>Kaynak Ajansı</h1>
<div class="sub">Node: {NODE_ID} — 8sn sonra yenilenir</div>
<div class="tun">🌐 {tunnel}</div>
<div class="grid">
  <div class="s">
    <div class="sv" style="color:{rc}">{info['ram']['cache_mb']}MB</div>
    <div class="sl">RAM Önbelleği</div>
    <div class="bw"><div class="b" style="width:{ram_pct}%;background:{rc}"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#8892a4;margin-top:3px"><span>%{ram_pct}</span><span>/{RAM_CACHE_MB}MB limit</span></div>
  </div>
  <div class="s">
    <div class="sv" style="color:{dc}">{info['disk']['store_gb']}GB</div>
    <div class="sl">Disk Deposu</div>
    <div class="bw"><div class="b" style="width:{disk_pct}%;background:{dc}"></div></div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#8892a4;margin-top:3px"><span>%{disk_pct}</span><span>/{DISK_LIMIT_GB}GB limit</span></div>
  </div>
</div>
<div class="services">
  <div class="svc"><div class="svc-ico">🧠</div><div class="svc-lbl">Cache Keys</div><div class="svc-val">{info['ram']['cache_keys']}</div></div>
  <div class="svc"><div class="svc-ico">📁</div><div class="svc-lbl">Dosyalar</div><div class="svc-val">{state['file_store']['files']}</div></div>
  <div class="svc"><div class="svc-ico">🔀</div><div class="svc-lbl">Proxy</div><div class="svc-val">{'AKTİF' if _proxy_state['active'] else 'Kapalı'}</div></div>
  <div class="svc"><div class="svc-ico">⚡</div><div class="svc-lbl">CPU Load</div><div class="svc-val">{info['cpu']['load1']}</div></div>
</div>
<a class="link" href="{MAIN_URL}" target="_blank">Ana Sunucuya Git</a>
</div></body></html>"""


@app.route("/api/status")
def api_status():
    return jsonify(_get_resource_info())


# ══════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════

for res, val in [
    (resource.RLIMIT_NOFILE, (65536, 65536)),
    (resource.RLIMIT_NPROC,  (resource.RLIM_INFINITY, resource.RLIM_INFINITY)),
]:
    try: resource.setrlimit(res, val)
    except: pass

print("\n" + "━"*54)
print("  ⚙️   Resource Agent v1.0")
print(f"      NODE_ID  : {NODE_ID}")
print(f"      MAIN_URL : {MAIN_URL}")
print(f"      RAM Cache: {RAM_CACHE_MB}MB")
print(f"      Disk     : {DISK_LIMIT_GB}GB")
print("━"*54)
print(f"  Servisler: RAM Cache | File Store | TCP Proxy | CPU Worker")
print("━"*54 + "\n")

threading.Thread(target=_start_tunnel,   daemon=True).start()
threading.Thread(target=_register_loop,  daemon=True).start()
threading.Thread(target=_keepalive_loop, daemon=True).start()

print(f"[Agent] Flask :{PORT} başlatılıyor...")
app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
