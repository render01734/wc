"""
⚙️  Resource Agent — v11.0  (Her zaman aktif)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MC sunucusunun açık/kapalı olması FARK ETMEZ.
Agent, ana sunucuya sürekli şunları sağlar:

  🧠 RAM Cache     → chunk/entity/data önbelleği
  💾 Disk Store    → region arşiv, yedek, plugin deposu
  🔀 TCP Proxy     → oyuncu bağlantı relay
  ⚡ CPU Worker    → sıkıştırma, hash, istatistik

Başlangıçta ana sunucuya kayıt olur.
Sonra her 20 saniyede bir heartbeat atar.
Ana sunucu cevap vermese bile bekler, tekrar dener.
"""

import os, sys, time, json, gzip, threading, hashlib, subprocess
import socket, queue, struct, shutil
from pathlib import Path
from collections import OrderedDict
from datetime import datetime
import urllib.request as _ur

from flask import Flask, request, jsonify, Response
import eventlet
eventlet.monkey_patch()

# ─── Konfigürasyon ────────────────────────────────────────────
PORT          = int(os.environ.get("PORT",          "8080"))
MAIN_URL      = os.environ.get("MAIN_URL",          "https://wc-tsgd.onrender.com").rstrip("/")
RAM_CACHE_MB  = int(os.environ.get("RAM_CACHE_MB",  "300"))
DISK_LIMIT_GB = float(os.environ.get("DISK_LIMIT_GB","10.0"))
MY_URL        = os.environ.get("RENDER_EXTERNAL_URL","").rstrip("/")
NODE_ID       = MY_URL.replace("https://","").replace(".onrender.com","") or f"agent-{PORT}"
AGENT_DATA    = Path(os.environ.get("AGENT_DATA", "/agent_data"))

AGENT_DATA.mkdir(parents=True, exist_ok=True)
for sub in ["regions/world","regions/world_nether","regions/world_the_end",
            "backups","plugins","configs","chunks"]:
    (AGENT_DATA / sub).mkdir(parents=True, exist_ok=True)

print(f"""
{'━'*52}
  ⚙️   Resource Agent v11.0
  NODE_ID  : {NODE_ID}
  MAIN_URL : {MAIN_URL}
  RAM Cache: {RAM_CACHE_MB}MB
  Disk     : {DISK_LIMIT_GB}GB
  Port     : {PORT}
{'━'*52}
""")


# ══════════════════════════════════════════════════════════════
#  1.  RAM CACHE  (LRU, gzip sıkıştırmalı)
# ══════════════════════════════════════════════════════════════

class RAMCache:
    def __init__(self, max_mb: int):
        self.max_bytes = max_mb * 1024 * 1024
        self._data: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = 0

    def set(self, key: str, value: bytes) -> bool:
        compressed = gzip.compress(value, compresslevel=1)
        with self._lock:
            if key in self._data:
                self._size -= len(self._data[key])
                del self._data[key]
            while self._size + len(compressed) > self.max_bytes and self._data:
                _, evicted = self._data.popitem(last=False)
                self._size -= len(evicted)
                self.evictions += 1
            if len(compressed) > self.max_bytes:
                return False
            self._data[key] = compressed
            self._size += len(compressed)
            self._data.move_to_end(key)
            return True

    def get(self, key: str) -> bytes | None:
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return gzip.decompress(self._data[key])

    def delete(self, key: str):
        with self._lock:
            if key in self._data:
                self._size -= len(self._data[key])
                del self._data[key]

    def flush(self, prefix: str = "") -> int:
        with self._lock:
            if not prefix:
                n = len(self._data)
                self._data.clear(); self._size = 0
                return n
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                self._size -= len(self._data[k])
                del self._data[k]
            return len(keys)

    def keys_with_prefix(self, prefix: str) -> list:
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "keys":      len(self._data),
            "used_mb":   round(self._size / 1024 / 1024, 2),
            "max_mb":    RAM_CACHE_MB,
            "hit_rate":  round(self.hits / total * 100, 1) if total else 0,
            "hits":      self.hits,
            "misses":    self.misses,
            "evictions": self.evictions,
        }


cache = RAMCache(RAM_CACHE_MB)


# ══════════════════════════════════════════════════════════════
#  2.  FILE STORE
# ══════════════════════════════════════════════════════════════

CATEGORIES = {"regions", "chunks", "backups", "plugins", "configs"}

def _store_path(category: str, *parts) -> Path:
    cat = category if category in CATEGORIES else "configs"
    return AGENT_DATA / cat / Path(*parts)

def store_used_gb() -> float:
    total = sum(f.stat().st_size for f in AGENT_DATA.rglob("*") if f.is_file())
    return round(total / 1e9, 3)

def store_free_gb() -> float:
    return round(DISK_LIMIT_GB - store_used_gb(), 3)

def store_stats() -> dict:
    cats = {}
    for cat in CATEGORIES:
        d = AGENT_DATA / cat
        files = list(d.rglob("*.mca")) + list(d.rglob("*")) if d.exists() else []
        files = [f for f in files if f.is_file()]
        cats[cat] = {"count": len(files),
                     "size_mb": round(sum(f.stat().st_size for f in files)/1e6, 1)}
    return {
        "used_gb":  store_used_gb(),
        "free_gb":  store_free_gb(),
        "limit_gb": DISK_LIMIT_GB,
        "categories": cats,
    }


# ══════════════════════════════════════════════════════════════
#  3.  TCP PROXY
# ══════════════════════════════════════════════════════════════

class TCPProxy:
    def __init__(self):
        self.active       = False
        self.target_host  = ""
        self.target_port  = 0
        self.listen_port  = 25565
        self._server_sock = None
        self._thread      = None
        self.connections  = 0
        self._lock        = threading.Lock()

    def start(self, host: str, port: int, listen_port: int = 25565) -> bool:
        with self._lock:
            if self.active:
                self.stop()
            self.target_host = host
            self.target_port = port
            self.listen_port = listen_port
            try:
                self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._server_sock.bind(("0.0.0.0", listen_port))
                self._server_sock.listen(50)
                self.active = True
                self._thread = threading.Thread(target=self._accept_loop, daemon=True)
                self._thread.start()
                return True
            except Exception as e:
                print(f"[Proxy] Başlatma hatası: {e}")
                self.active = False
                return False

    def stop(self):
        self.active = False
        if self._server_sock:
            try: self._server_sock.close()
            except: pass
            self._server_sock = None

    def _accept_loop(self):
        while self.active:
            try:
                conn, addr = self._server_sock.accept()
                t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
                t.start()
            except: break

    def _handle(self, client: socket.socket):
        try:
            server = socket.create_connection((self.target_host, self.target_port), timeout=10)
            self.connections += 1
            def fwd(src, dst):
                try:
                    while True:
                        d = src.recv(4096)
                        if not d: break
                        dst.sendall(d)
                except: pass
                finally:
                    try: src.close()
                    except: pass
                    try: dst.close()
                    except: pass
            threading.Thread(target=fwd, args=(client, server), daemon=True).start()
            threading.Thread(target=fwd, args=(server, client), daemon=True).start()
        except:
            try: client.close()
            except: pass
        finally:
            self.connections = max(0, self.connections - 1)

    def status(self) -> dict:
        return {
            "active":      self.active,
            "target":      f"{self.target_host}:{self.target_port}",
            "listen_port": self.listen_port,
            "connections": self.connections,
        }


proxy = TCPProxy()


# ══════════════════════════════════════════════════════════════
#  4.  CPU WORKER
# ══════════════════════════════════════════════════════════════

class CPUWorker:
    ALLOWED_CMDS = {"gzip","zstd","sha256sum","md5sum","find","du","wc","sort","uniq"}

    def __init__(self):
        self._q:   queue.Queue           = queue.Queue()
        self._res: dict[str, dict]       = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, task_type: str, payload: dict) -> str:
        tid = hashlib.md5(f"{task_type}{payload}{time.time()}".encode()).hexdigest()[:12]
        with self._lock:
            self._res[tid] = {"status": "pending"}
        self._q.put((tid, task_type, payload))
        return tid

    def result(self, tid: str) -> dict:
        with self._lock:
            return self._res.get(tid, {"status": "not_found"})

    def _loop(self):
        while True:
            tid, task_type, payload = self._q.get()
            try:
                result = self._run(task_type, payload)
                with self._lock:
                    self._res[tid] = {"status": "done", "result": result}
            except Exception as e:
                with self._lock:
                    self._res[tid] = {"status": "error", "error": str(e)}
            # Sonuçları 5 dakika sonra temizle
            threading.Timer(300, lambda t=tid: self._res.pop(t, None)).start()

    def _run(self, task_type: str, payload: dict):
        if task_type == "compress_file":
            src  = Path(payload["path"])
            dest = Path(payload.get("dest", str(src) + ".gz"))
            with open(src,"rb") as f, gzip.open(dest,"wb",compresslevel=6) as g:
                shutil.copyfileobj(f, g)
            return {"dest": str(dest), "size": dest.stat().st_size}

        elif task_type == "decompress_file":
            src  = Path(payload["path"])
            dest = Path(payload.get("dest", str(src).removesuffix(".gz")))
            with gzip.open(src,"rb") as f, open(dest,"wb") as g:
                shutil.copyfileobj(f, g)
            return {"dest": str(dest)}

        elif task_type == "hash_files":
            root = Path(payload.get("path", str(AGENT_DATA)))
            pat  = payload.get("pattern", "*.mca")
            out  = {}
            for f in root.rglob(pat):
                if f.is_file():
                    h = hashlib.md5(f.read_bytes()).hexdigest()
                    out[str(f.relative_to(root))] = h
            return out

        elif task_type == "storage_stats":
            return store_stats()

        elif task_type == "cache_stats":
            return cache.stats()

        elif task_type == "disk_usage":
            total = shutil.disk_usage("/")
            return {"total_gb": round(total.total/1e9,2),
                    "used_gb":  round(total.used/1e9,2),
                    "free_gb":  round(total.free/1e9,2)}

        elif task_type == "run_command":
            cmd = payload.get("cmd","").split()[0]
            if cmd not in self.ALLOWED_CMDS:
                raise ValueError(f"İzin verilmeyen komut: {cmd}")
            r = subprocess.run(payload["cmd"], shell=True, capture_output=True, timeout=30)
            return {"stdout": r.stdout.decode()[:4096], "rc": r.returncode}

        elif task_type == "echo":
            return payload

        else:
            raise ValueError(f"Bilinmeyen görev: {task_type}")


worker = CPUWorker()


# ══════════════════════════════════════════════════════════════
#  5.  HEARTBEAT  — Ana sunucuya sürekli bildir
# ══════════════════════════════════════════════════════════════

def _build_info() -> dict:
    import psutil
    mem  = psutil.virtual_memory()
    swp  = psutil.swap_memory()
    disk = shutil.disk_usage("/")
    return {
        "node_id": NODE_ID,
        "tunnel":  MY_URL,
        "ram": {
            "free_mb":    int(mem.available / 1024 / 1024),
            "total_mb":   int(mem.total     / 1024 / 1024),
            "cache_mb":   cache.stats()["used_mb"],
            "swap_free":  int(swp.free / 1024 / 1024),
        },
        "disk": {
            "free_gb":    store_free_gb(),
            "used_gb":    store_used_gb(),
            "limit_gb":   DISK_LIMIT_GB,
            "sys_free_gb": round(disk.free / 1e9, 2),
        },
        "cpu": {
            "cores":  psutil.cpu_count(),
            "load1":  round(psutil.getloadavg()[0], 2),
            "load5":  round(psutil.getloadavg()[1], 2),
        },
        "proxy":    proxy.status(),
        "cache":    cache.stats(),
        "ts":       int(time.time()),
        "version":  "11.0",
    }


def _heartbeat_loop():
    """
    Sürekli çalışır.
    Ana sunucu kapalı olsa bile döngü durmaz — tekrar dener.
    İlk kayıtta /api/agent/register, sonra /api/agent/heartbeat kullanır.
    """
    registered = False
    fails      = 0

    while True:
        info     = _build_info()
        endpoint = "/api/agent/register" if not registered else "/api/agent/heartbeat"
        try:
            req = _ur.Request(
                MAIN_URL + endpoint,
                data=json.dumps(info).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
            if resp.get("ok"):
                if not registered:
                    print(f"[Heartbeat] ✅ Ana sunucuya kayıt olundu: {MAIN_URL}")
                registered = True
                fails      = 0
            else:
                print(f"[Heartbeat] ⚠️  Sunucu red döndü: {resp}")
        except Exception as e:
            fails += 1
            # İlk 3 başarısızlıkta kısa bekleme, sonra 60sn
            wait = 10 if fails <= 3 else 60
            if fails == 1 or fails % 5 == 0:
                print(f"[Heartbeat] Ana sunucuya ulaşılamıyor ({fails}. deneme) — {wait}sn sonra tekrar. Hata: {e}")
            # Kayıt başarısız olduysa her seferinde tekrar dene
            if fails > 5:
                registered = False   # Yeniden kayıt dene
        # 20 saniyede bir heartbeat (MC açık/kapalı fark etmez)
        time.sleep(20)


# ══════════════════════════════════════════════════════════════
#  FLASK API
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)

# ── Sağlık / Durum ────────────────────────────────────────────

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok", "node": NODE_ID, "version": "11.0"})

@app.route("/api/status")
def api_status():
    return jsonify(_build_info())


# ── RAM Cache ─────────────────────────────────────────────────

@app.route("/api/cache/set", methods=["POST"])
def cache_set():
    key = request.args.get("key") or (request.json or {}).get("key","")
    if not key:
        return jsonify({"ok": False, "error": "key gerekli"}), 400
    data = request.get_data() or json.dumps(request.json).encode()
    ok   = cache.set(key, data)
    return jsonify({"ok": ok, "key": key, "size": len(data)})

@app.route("/api/cache/get/<path:key>")
def cache_get(key):
    val = cache.get(key)
    if val is None:
        return jsonify({"ok": False}), 404
    return Response(val, mimetype="application/octet-stream")

@app.route("/api/cache/delete/<path:key>", methods=["DELETE","POST"])
def cache_delete(key):
    cache.delete(key)
    return jsonify({"ok": True})

@app.route("/api/cache/flush", methods=["POST"])
def cache_flush():
    prefix = (request.json or {}).get("prefix","")
    n = cache.flush(prefix)
    return jsonify({"ok": True, "flushed": n})

@app.route("/api/cache/stats")
def cache_stats():
    return jsonify(cache.stats())

@app.route("/api/cache/keys")
def cache_keys():
    prefix = request.args.get("prefix","")
    return jsonify({"keys": cache.keys_with_prefix(prefix)})


# ── File Store ────────────────────────────────────────────────

@app.route("/api/files/<category>/<path:filename>", methods=["PUT"])
def file_put(category, filename):
    if store_used_gb() >= DISK_LIMIT_GB * 0.95:
        return jsonify({"ok": False, "error": "Disk dolmak üzere"}), 507
    p = _store_path(category, filename)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(request.get_data())
    return jsonify({"ok": True, "path": str(p.relative_to(AGENT_DATA)), "size": p.stat().st_size})

@app.route("/api/files/<category>/<path:filename>", methods=["GET"])
def file_get(category, filename):
    p = _store_path(category, filename)
    if not p.exists():
        return jsonify({"ok": False, "error": "Bulunamadı"}), 404
    return Response(p.read_bytes(), mimetype="application/octet-stream")

@app.route("/api/files/<category>/<path:filename>/exists")
def file_exists(category, filename):
    p = _store_path(category, filename)
    return jsonify({"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0})

@app.route("/api/files/<category>/<path:filename>", methods=["DELETE"])
def file_delete(category, filename):
    p = _store_path(category, filename)
    if p.exists(): p.unlink()
    return jsonify({"ok": True})

@app.route("/api/files/<category>")
def file_list(category):
    d = AGENT_DATA / (category if category in CATEGORIES else "configs")
    if not d.exists():
        return jsonify({"files": []})
    files = [{"name": f.name,
              "path": str(f.relative_to(AGENT_DATA)),
              "size": f.stat().st_size,
              "modified": int(f.stat().st_mtime)}
             for f in sorted(d.rglob("*")) if f.is_file()]
    return jsonify({"files": files, "count": len(files)})

@app.route("/api/files/storage/stats")
def file_storage_stats():
    return jsonify(store_stats())


# ── TCP Proxy ─────────────────────────────────────────────────

@app.route("/api/proxy/start", methods=["POST"])
def proxy_start():
    d    = request.json or {}
    host = d.get("host","127.0.0.1")
    port = int(d.get("port", 25565))
    lp   = int(d.get("listen_port", 25565))
    ok   = proxy.start(host, port, lp)
    return jsonify({"ok": ok, **proxy.status()})

@app.route("/api/proxy/stop", methods=["POST"])
def proxy_stop():
    proxy.stop()
    return jsonify({"ok": True})

@app.route("/api/proxy/status")
def proxy_status():
    return jsonify(proxy.status())


# ── CPU Worker ────────────────────────────────────────────────

@app.route("/api/cpu/submit", methods=["POST"])
def cpu_submit():
    d    = request.json or {}
    tid  = worker.submit(d.get("type","echo"), d.get("payload", {}))
    return jsonify({"ok": True, "task_id": tid})

@app.route("/api/cpu/result/<tid>")
def cpu_result(tid):
    return jsonify(worker.result(tid))


# ── Toplu işlem (ana sunucu için) ─────────────────────────────

@app.route("/api/bulk/cache_and_store", methods=["POST"])
def bulk_cache_and_store():
    """
    Ana sunucu birden fazla key'i aynı anda gönderebilir.
    body: {"items": [{"key": "...", "data_b64": "...base64..."}, ...]}
    """
    import base64
    d     = request.json or {}
    items = d.get("items", [])
    ok    = 0
    for item in items:
        key  = item.get("key","")
        data = base64.b64decode(item.get("data_b64",""))
        if key and data and cache.set(key, data):
            ok += 1
    return jsonify({"ok": True, "stored": ok, "total": len(items)})


# ══════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════

# Heartbeat thread — hemen başlar, hiç durmaz
threading.Thread(target=_heartbeat_loop, daemon=True).start()

if __name__ == "__main__":
    print(f"[Agent] :{PORT} başlatılıyor...")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
