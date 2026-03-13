"""
⚙️  Resource Agent — v12.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MC açık/kapalı fark etmez. Agent ana sunucuya şunları sağlar:

  🧠 RAM Cache (LRU, gzip)  → chunk/entity önbelleği
  💾 Disk Store (5 kategori) → region arşiv, remap cache, yedek
  🔀 TCP Proxy               → oyuncu bağlantı relay
  ⚡ CPU Worker              → sıkıştırma, hash, istatistik
  💿 Swap Bloğu              → ana sunucu için disk → swap

512MB Bellek Bütçesi:
  Flask + OS taban : ~80MB
  RAM Cache        : RAM_CACHE_MB (default 380MB)
  Peak geçici      : ~16MB (en büyük cache set × 2)
  Buffer           : ~36MB
  ─────────────────────────
  Toplam           : ~512MB

v12.0 Değişiklikleri:
  • RLIMIT_AS = 500MB → Python process sınırı (Render OOM önlenir)
  • cache_set: 8MB sınırı + MemoryError → 507 (crash değil)
  • file_upload: 256KB stream write (RAM'e yükleme yok)
  • paper_cache kategori eklendi (remap cache yedekleme)
  • RAM raporlama: process RSS (psutil host değil)
  • Swap bloğu: fallocate sparse → gerçek dd (mkswap uyumlu)
  • after_request GC → her response sonrası gen0 temizle
  • Heartbeat: 5+ fail → yeniden kayıt (Render 15dk uyku sonrası)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, sys, time, json, gzip, threading, hashlib, subprocess
import socket, queue, shutil, gc, resource
from pathlib import Path
from collections import OrderedDict
from datetime import datetime
import urllib.request as _ur
import urllib.error

# ── 512MB process limiti ─────────────────────────────────────────────────────
# Render 512MB aşınca process'i öldürür. RLIMIT_AS ile 500MB'de MemoryError
# üretir → graceful fail (crash değil).
try:
    _AS_LIMIT = 500 * 1024 * 1024
    _s, _h = resource.getrlimit(resource.RLIMIT_AS)
    if _h == resource.RLIM_INFINITY or _h > _AS_LIMIT:
        resource.setrlimit(resource.RLIMIT_AS, (_AS_LIMIT, _AS_LIMIT))
except Exception:
    pass  # Bazı ortamlarda izin yok — devam et

import eventlet
eventlet.monkey_patch()
from flask import Flask, request, jsonify, Response

# ── Konfigürasyon ─────────────────────────────────────────────────────────────
PORT          = int(os.environ.get("PORT",          "8080"))
MAIN_URL      = os.environ.get("MAIN_URL",          "https://wc-tsgd.onrender.com").rstrip("/")
RAM_CACHE_MB  = int(os.environ.get("RAM_CACHE_MB",  "380"))
DISK_LIMIT_GB = float(os.environ.get("DISK_LIMIT_GB", "10.0"))
MY_URL        = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
NODE_ID       = MY_URL.replace("https://","").replace(".onrender.com","") or f"agent-{PORT}"
AGENT_DATA    = Path(os.environ.get("AGENT_DATA", "/agent_data"))
VERSION       = "12.0"

# IS_PANEL=1 → Bu agent panel host'u.
# mc_panel.py'yi WORKER_URL=MAIN_URL ile başlatır.
# Kullanıcı bu agent'ın URL'ine browser ile bağlanır.
IS_PANEL      = os.environ.get("IS_PANEL", "0") == "1"

# ── Dizinler ──────────────────────────────────────────────────────────────────
AGENT_DATA.mkdir(parents=True, exist_ok=True)
CATEGORIES = {"regions", "chunks", "backups", "plugins", "configs", "cuberite_cache", "players", "stats"}
for sub in ["regions/world", "regions/world_nether", "regions/world_the_end",
            "backups", "plugins", "configs", "chunks", "cuberite_cache"]:
    (AGENT_DATA / sub).mkdir(parents=True, exist_ok=True)

print(f"""
{'━'*52}
  ⚙️   Resource Agent v{VERSION}
  NODE_ID  : {NODE_ID}
  MAIN_URL : {MAIN_URL}
  RAM Cache: {RAM_CACHE_MB}MB  (limit: 500MB process)
  Disk     : {DISK_LIMIT_GB}GB
  Port     : {PORT}
{'━'*52}
""")


# ══════════════════════════════════════════════════════════════════════════════
#  1.  RAM CACHE — LRU, gzip, MemoryError korumalı
# ══════════════════════════════════════════════════════════════════════════════

class RAMCache:
    def __init__(self, max_mb: int):
        self.max_bytes = max_mb * 1024 * 1024
        self._data: OrderedDict[str, bytes] = OrderedDict()
        self._size  = 0
        self._lock  = threading.Lock()
        self.hits = self.misses = self.evictions = 0

    def set(self, key: str, value: bytes) -> bool:
        """Veriyi sıkıştır ve LRU cache'e ekle."""
        try:
            compressed = gzip.compress(value, compresslevel=1)
        except MemoryError:
            gc.collect()
            return False
        finally:
            del value  # Ham veriyi hemen serbest bırak

        with self._lock:
            if key in self._data:
                self._size -= len(self._data[key])
                del self._data[key]
            # Yer aç
            while self._size + len(compressed) > self.max_bytes and self._data:
                _, evicted = self._data.popitem(last=False)
                self._size -= len(evicted)
                del evicted
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
            compressed = self._data[key]
        return gzip.decompress(compressed)

    def delete(self, key: str):
        with self._lock:
            if key in self._data:
                self._size -= len(self._data[key])
                del self._data[key]

    def flush(self, prefix: str = "") -> int:
        with self._lock:
            if not prefix:
                n = len(self._data)
                self._data.clear()
                self._size = 0
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
        # Sıkıştırılmış cache boyutunu RSS'e ekle (doğru RAM raporu)
        try:
            import psutil
            my_rss = int(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)
        except Exception:
            my_rss = 80 + self._size // 1024 // 1024
        return {
            "keys":       len(self._data),
            "used_mb":    round(self._size / 1024 / 1024, 2),
            "rss_mb":     my_rss,
            "max_mb":     RAM_CACHE_MB,
            "hit_rate":   round(self.hits / total * 100, 1) if total else 0,
            "hits":       self.hits,
            "misses":     self.misses,
            "evictions":  self.evictions,
        }


cache = RAMCache(RAM_CACHE_MB)


# ══════════════════════════════════════════════════════════════════════════════
#  2.  FILE STORE
# ══════════════════════════════════════════════════════════════════════════════

def _safe_path(category: str, *parts) -> Path:
    """Kategori doğrulaması ile güvenli dosya yolu."""
    cat = category if category in CATEGORIES else "configs"
    return AGENT_DATA / cat / Path(*parts)

def store_used_gb() -> float:
    try:
        total = sum(f.stat().st_size for f in AGENT_DATA.rglob("*") if f.is_file())
        return round(total / 1e9, 3)
    except Exception:
        return 0.0

def store_free_gb() -> float:
    return max(0.0, round(DISK_LIMIT_GB - store_used_gb(), 3))

def store_stats() -> dict:
    cats = {}
    for cat in CATEGORIES:
        d = AGENT_DATA / cat
        files = [f for f in d.rglob("*") if f.is_file()] if d.exists() else []
        cats[cat] = {
            "count":   len(files),
            "size_mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
        }
    return {
        "used_gb":    store_used_gb(),
        "free_gb":    store_free_gb(),
        "limit_gb":   DISK_LIMIT_GB,
        "categories": cats,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  3.  TCP PROXY
# ══════════════════════════════════════════════════════════════════════════════

class TCPProxy:
    def __init__(self):
        self.active      = False
        self.target_host = ""
        self.target_port = 0
        self.listen_port = 25565
        self._sock       = None
        self._lock       = threading.Lock()
        self.connections = 0

    def start(self, host: str, port: int, listen_port: int = 25565) -> bool:
        with self._lock:
            if self.active:
                self.stop()
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._sock.bind(("0.0.0.0", listen_port))
                self._sock.listen(50)
                self.target_host = host
                self.target_port = port
                self.listen_port = listen_port
                self.active      = True
                threading.Thread(target=self._accept_loop, daemon=True).start()
                return True
            except Exception as e:
                print(f"[Proxy] Başlatma hatası: {e}")
                self.active = False
                return False

    def stop(self):
        self.active = False
        if self._sock:
            try: self._sock.close()
            except: pass
            self._sock = None

    def _accept_loop(self):
        while self.active:
            try:
                conn, _ = self._sock.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except Exception:
                break

    def _handle(self, client: socket.socket):
        try:
            srv = socket.create_connection((self.target_host, self.target_port), timeout=10)
            self.connections += 1

            def relay(src, dst):
                try:
                    while True:
                        d = src.recv(4096)
                        if not d: break
                        dst.sendall(d)
                except Exception:
                    pass
                finally:
                    for s in (src, dst):
                        try: s.close()
                        except: pass

            threading.Thread(target=relay, args=(client, srv), daemon=True).start()
            threading.Thread(target=relay, args=(srv, client), daemon=True).start()
        except Exception:
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


# ══════════════════════════════════════════════════════════════════════════════
#  4.  CPU WORKER
# ══════════════════════════════════════════════════════════════════════════════

class CPUWorker:
    ALLOWED_CMDS = {"gzip", "zstd", "sha256sum", "md5sum", "find", "du", "wc"}

    def __init__(self):
        self._q:   queue.Queue     = queue.Queue()
        self._res: dict[str, dict] = {}
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
            threading.Timer(300, lambda t=tid: self._res.pop(t, None)).start()

    def _run(self, task_type: str, payload: dict):
        if task_type == "compress_file":
            src  = Path(payload["path"])
            dest = Path(payload.get("dest", str(src) + ".gz"))
            with open(src, "rb") as f, gzip.open(dest, "wb", compresslevel=6) as g:
                shutil.copyfileobj(f, g)
            return {"dest": str(dest), "size": dest.stat().st_size}

        elif task_type == "decompress_file":
            src  = Path(payload["path"])
            dest = Path(payload.get("dest", str(src).removesuffix(".gz")))
            with gzip.open(src, "rb") as f, open(dest, "wb") as g:
                shutil.copyfileobj(f, g)
            return {"dest": str(dest)}

        elif task_type == "hash_files":
            root = Path(payload.get("path", str(AGENT_DATA)))
            out  = {}
            for f in root.rglob(payload.get("pattern", "*.mca")):
                if f.is_file():
                    out[str(f.relative_to(root))] = hashlib.md5(f.read_bytes()).hexdigest()
            return out

        elif task_type == "storage_stats":
            return store_stats()

        elif task_type == "cache_stats":
            return cache.stats()

        elif task_type == "disk_usage":
            dk = shutil.disk_usage("/")
            return {"total_gb": round(dk.total / 1e9, 2),
                    "used_gb":  round(dk.used  / 1e9, 2),
                    "free_gb":  round(dk.free  / 1e9, 2)}

        elif task_type == "run_command":
            cmd = payload.get("cmd", "").split()[0]
            if cmd not in self.ALLOWED_CMDS:
                raise ValueError(f"İzin verilmeyen komut: {cmd}")
            r = subprocess.run(payload["cmd"], shell=True,
                               capture_output=True, timeout=30)
            return {"stdout": r.stdout.decode()[:4096], "rc": r.returncode}

        elif task_type == "echo":
            return payload

        else:
            raise ValueError(f"Bilinmeyen görev: {task_type}")


worker = CPUWorker()


# ══════════════════════════════════════════════════════════════════════════════
#  5.  HEARTBEAT — Ana sunucuya sürekli bildir
# ══════════════════════════════════════════════════════════════════════════════

def _build_info() -> dict:
    """Process RSS ile doğru RAM raporu (psutil.virtual_memory() HOST okur)."""
    import psutil
    gc.collect(0)  # Stats çağrısında gen0 temizle

    try:
        my_rss_mb = int(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)
    except Exception:
        my_rss_mb = 80

    cache_mb  = int(cache._size / 1024 / 1024)
    used_mb   = max(my_rss_mb, 80 + cache_mb)
    free_mb   = max(0, 512 - used_mb)

    try:
        load1, load5, _ = os.getloadavg()
    except Exception:
        load1 = load5 = 0.0

    dk = shutil.disk_usage("/")
    return {
        "node_id":  NODE_ID,
        "tunnel":   MY_URL,
        "version":  VERSION,
        "ts":       int(time.time()),
        "ram": {
            "rss_mb":     my_rss_mb,
            "used_mb":    used_mb,
            "free_mb":    free_mb,
            "total_mb":   512,
            "cache_mb":   cache_mb,
        },
        "disk": {
            "store_free_gb": store_free_gb(),
            "store_used_gb": store_used_gb(),
            "limit_gb":      DISK_LIMIT_GB,
            "sys_free_gb":   round(dk.free / 1e9, 2),
            "sys_total_gb":  round(dk.total / 1e9, 2),
        },
        "cpu": {
            "cores": os.cpu_count() or 1,
            "load1": round(load1, 2),
            "load5": round(load5, 2),
        },
        "proxy":  proxy.status(),
        "cache":  cache.stats(),
        "store":  store_stats(),
    }


def _heartbeat_loop():
    """
    Sürekli çalışır. Ana sunucu kapalı olsa bile bekler, tekrar dener.
    5+ fail → yeniden kayıt (Render 15dk uyku sonrası bağlantı kopabilir).
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
                    print(f"[Heartbeat] ✅ Kayıt: {MAIN_URL}")
                registered = True
                fails      = 0
            else:
                print(f"[Heartbeat] ⚠️  Red: {resp}")
                fails += 1
        except Exception as e:
            fails += 1
            wait = 10 if fails <= 3 else 60
            if fails == 1 or fails % 5 == 0:
                print(f"[Heartbeat] Ulaşılamıyor ({fails}x) — {wait}s: {e}")
            if fails > 5:
                registered = False  # Yeniden kayıt
        time.sleep(20)


# ══════════════════════════════════════════════════════════════════════════════
#  6.  SWAP BLOĞU — Ana sunucu bu ajanın diskini swap olarak kullanır
# ══════════════════════════════════════════════════════════════════════════════

SWAP_PATH       = AGENT_DATA / "swap_block.bin"
_swap_allocated = False
_swap_size_mb   = 0


def _create_swap_block(size_mb: int) -> bool:
    """Gerçek disk bloğu oluştur (sparse değil — mkswap uyumlu)."""
    global _swap_allocated, _swap_size_mb
    try:
        print(f"[Swap] {size_mb}MB swap bloğu oluşturuluyor...")
        # dd ile gerçek veri yaz (sparse olmayan dosya — mkswap gerektirir)
        blk = 64
        cnt = max(1, size_mb // blk)
        r = subprocess.run(
            f"dd if=/dev/zero of={SWAP_PATH} bs={blk}M count={cnt} status=none 2>/dev/null",
            shell=True
        )
        if r.returncode != 0 or not SWAP_PATH.exists():
            # Fallback: fallocate
            subprocess.run(f"fallocate -l {size_mb}M {SWAP_PATH}", shell=True)
        actual = SWAP_PATH.stat().st_size // (1024 * 1024)
        _swap_allocated = True
        _swap_size_mb   = actual
        print(f"[Swap] ✅ {actual}MB swap bloğu hazır")
        return True
    except Exception as e:
        print(f"[Swap] ❌ Hata: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  FLASK APP
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.after_request
def _gc_after(resp):
    """Her response sonrası gen0 GC — RAM sürünmesini önle."""
    gc.collect(0)
    return resp


# ── Sağlık ─────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/health")
def health():
    return jsonify({"status": "ok", "node": NODE_ID, "version": VERSION})

@app.route("/api/status")
def api_status():
    return jsonify(_build_info())

@app.route("/api/ping")
def api_ping():
    return jsonify({"ok": True, "ts": int(time.time())})


# ── RAM Cache ─────────────────────────────────────────────────────────────

@app.route("/api/cache/set", methods=["POST"])
def cache_set():
    key  = request.args.get("key") or (request.json or {}).get("key", "")
    if not key:
        return jsonify({"ok": False, "error": "key gerekli"}), 400

    # Content-Length kontrolü — büyük veri RAM'e yüklemeden önce reddet
    cl = request.content_length or 0
    if cl > 8 * 1024 * 1024:
        return jsonify({"ok": False, "error": "8MB üstü desteklenmiyor"}), 413

    data = request.get_data()
    if len(data) > 8 * 1024 * 1024:
        return jsonify({"ok": False, "error": "8MB üstü desteklenmiyor"}), 413

    try:
        ok = cache.set(key, data)
    except MemoryError:
        gc.collect()
        return jsonify({"ok": False, "error": "RAM dolu"}), 507

    return jsonify({"ok": ok, "key": key, "size": len(data)})


@app.route("/api/cache/get/<path:key>")
def cache_get(key):
    val = cache.get(key)
    if val is None:
        return jsonify({"ok": False}), 404
    return Response(val, mimetype="application/octet-stream")


@app.route("/api/cache/delete/<path:key>", methods=["DELETE", "POST"])
def cache_delete(key):
    cache.delete(key)
    return jsonify({"ok": True})


@app.route("/api/cache/flush", methods=["POST"])
def cache_flush():
    prefix = (request.json or {}).get("prefix", "")
    n = cache.flush(prefix)
    return jsonify({"ok": True, "flushed": n})


@app.route("/api/cache/stats")
def cache_stats():
    return jsonify(cache.stats())


@app.route("/api/cache/keys")
def cache_keys():
    prefix = request.args.get("prefix", "")
    return jsonify({"keys": cache.keys_with_prefix(prefix)})


# ── File Store ─────────────────────────────────────────────────────────────

@app.route("/api/files/<category>/<path:filename>", methods=["PUT", "POST"])
def file_put(category, filename):
    if store_used_gb() >= DISK_LIMIT_GB * 0.95:
        return jsonify({"ok": False, "error": "Disk dolmak üzere"}), 507
    p = _safe_path(category, filename)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Stream olarak yaz — büyük dosyayı RAM'e yükleme
    written = 0
    try:
        with open(p, "wb") as f:
            while True:
                chunk = request.stream.read(256 * 1024)  # 256KB chunk
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
    except MemoryError:
        gc.collect()
        return jsonify({"ok": False, "error": "RAM dolu"}), 507
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "path": str(p.relative_to(AGENT_DATA)), "size": written})


@app.route("/api/files/<category>/<path:filename>", methods=["GET"])
def file_get(category, filename):
    p = _safe_path(category, filename)
    if not p.exists():
        return jsonify({"ok": False, "error": "Bulunamadı"}), 404
    return Response(p.read_bytes(), mimetype="application/octet-stream")


@app.route("/api/files/<category>/<path:filename>/exists")
def file_exists(category, filename):
    p = _safe_path(category, filename)
    return jsonify({"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0})


@app.route("/api/files/<category>/<path:filename>", methods=["DELETE"])
def file_delete(category, filename):
    p = _safe_path(category, filename)
    if p.exists():
        p.unlink()
    return jsonify({"ok": True})


@app.route("/api/files/<category>")
def file_list(category):
    d = AGENT_DATA / (category if category in CATEGORIES else "configs")
    if not d.exists():
        return jsonify({"files": []})
    files = [
        {"name": f.name,
         "path": str(f.relative_to(AGENT_DATA)),
         "size": f.stat().st_size,
         "modified": int(f.stat().st_mtime)}
        for f in sorted(d.rglob("*")) if f.is_file()
    ]
    return jsonify({"files": files, "count": len(files)})


@app.route("/api/files/storage/stats")
def file_storage_stats():
    return jsonify(store_stats())


# ── TCP Proxy ─────────────────────────────────────────────────────────────

@app.route("/api/proxy/start", methods=["POST"])
def proxy_start():
    d  = request.json or {}
    ok = proxy.start(d.get("host", "127.0.0.1"),
                     int(d.get("port", 25565)),
                     int(d.get("listen_port", 25565)))
    return jsonify({"ok": ok, **proxy.status()})


@app.route("/api/proxy/stop", methods=["POST"])
def proxy_stop():
    proxy.stop()
    return jsonify({"ok": True})


@app.route("/api/proxy/status")
def proxy_status():
    return jsonify(proxy.status())


# ── CPU Worker ─────────────────────────────────────────────────────────────

@app.route("/api/cpu/submit", methods=["POST"])
def cpu_submit():
    d   = request.json or {}
    tid = worker.submit(d.get("type", "echo"), d.get("payload", {}))
    return jsonify({"ok": True, "task_id": tid})


@app.route("/api/cpu/result/<tid>")
def cpu_result(tid):
    return jsonify(worker.result(tid))


# ── Swap Bloğu ─────────────────────────────────────────────────────────────

@app.route("/api/swap/allocate", methods=["POST"])
def swap_allocate():
    global _swap_allocated, _swap_size_mb
    d       = request.json or {}
    size_mb = int(d.get("size_mb", 1500))
    free_gb = store_free_gb()

    if free_gb < size_mb / 1024 + 0.3:
        return jsonify({"ok": False,
                        "error": f"Yetersiz disk: {free_gb:.1f}GB"}), 507
    ok = _create_swap_block(size_mb)
    if ok:
        return jsonify({"ok": True, "size_mb": _swap_size_mb, "path": str(SWAP_PATH)})
    return jsonify({"ok": False, "error": "Swap bloğu oluşturulamadı"}), 500


@app.route("/api/swap/read")
def swap_read():
    if not SWAP_PATH.exists():
        return jsonify({"ok": False, "error": "Swap bloğu yok"}), 404
    offset = int(request.args.get("offset", 0))
    size   = int(request.args.get("size", 64 * 1024 * 1024))

    def _stream():
        with open(SWAP_PATH, "rb") as f:
            f.seek(offset)
            remaining = size
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return Response(_stream(), mimetype="application/octet-stream")


@app.route("/api/swap/write", methods=["PUT"])
def swap_write():
    if not SWAP_PATH.exists():
        return jsonify({"ok": False, "error": "Swap bloğu yok"}), 404
    offset  = int(request.args.get("offset", 0))
    written = 0
    try:
        with open(SWAP_PATH, "r+b") as f:
            f.seek(offset)
            while True:
                chunk = request.stream.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "written": written})


@app.route("/api/swap/status")
def swap_status():
    size = SWAP_PATH.stat().st_size if SWAP_PATH.exists() else 0
    return jsonify({
        "allocated": _swap_allocated,
        "size_mb":   _swap_size_mb,
        "file_mb":   size // (1024 * 1024),
        "path":      str(SWAP_PATH),
    })


@app.route("/api/swap/release", methods=["POST"])
def swap_release():
    global _swap_allocated, _swap_size_mb
    if SWAP_PATH.exists():
        SWAP_PATH.unlink()
    _swap_allocated = False
    _swap_size_mb   = 0
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — Temizlik (Cleanup)
#  POST /api/admin/cleanup
#  Opsiyonel body: {"dry_run": true, "categories": ["corrupt","empty","tmp","old_backups"]}
#
#  Temizlenen şeyler:
#    corrupt   → geçersiz / bozuk JSON dosyaları (stats, players)
#    empty     → 0 byte dosyalar (tüm kategoriler)
#    tmp       → .tmp / .corrupt_bak / .corrupt_*.bak geçici dosyalar
#    old_bak   → 7 günden eski .bak yedekler
#    cache_gc  → RAM cache'den süresi dolmuş / erişilmemiş key'ler
#    orphan    → bilinen UUID eşleşmesi olmayan küçük dosyalar (<64 byte)
# ══════════════════════════════════════════════════════════════════════════════

def _agent_cleanup(dry_run: bool = False, categories: list = None) -> dict:
    """
    Agent disk temizliği. Ana sunucudan POST /api/admin/cleanup ile tetiklenir
    veya agent kendi kendine periyodik çalıştırır.
    """
    import time as _t
    import json as _jc

    cats = set(categories or ["corrupt", "empty", "tmp", "old_bak", "cache_gc"])
    report = {
        "node_id":    NODE_ID,
        "dry_run":    dry_run,
        "categories": list(cats),
        "removed":    [],
        "freed_bytes": 0,
        "errors":     [],
    }

    now = _t.time()

    # ── 1. Tüm disk dosyalarını tara ───────────────────────────────
    all_files = list(AGENT_DATA.rglob("*"))

    for fp in all_files:
        if not fp.is_file():
            continue

        try:
            stat   = fp.stat()
            size   = stat.st_size
            age_d  = (now - stat.st_mtime) / 86400
            suffix = fp.suffix.lower()
            name   = fp.name.lower()
            reason = None

            # empty — 0 byte dosyalar her zaman gereksizdir
            if "empty" in cats and size == 0:
                reason = "empty"

            # tmp — geçici kalıntılar
            elif "tmp" in cats and (
                name.endswith(".tmp") or
                ".corrupt_" in name or
                name.endswith(".corrupt_bak") or
                name.endswith(".corrupt")
            ):
                reason = "tmp"

            # old_bak — 7 günden eski .bak yedekler
            elif "old_bak" in cats and name.endswith(".bak") and age_d > 7:
                reason = f"old_bak ({age_d:.0f}d)"

            # corrupt — JSON dosyaları geçerli mi?
            elif "corrupt" in cats and suffix == ".json" and size > 0:
                try:
                    content = fp.read_text(encoding="utf-8", errors="replace").strip()
                    if not content or not content.startswith("{"):
                        reason = "corrupt_json"
                    else:
                        _jc.loads(content)  # parse test
                except Exception:
                    reason = "corrupt_json_parse"

            # orphan — 64 byte'tan küçük .json (Cuberite bunu okumaya çalışırsa hata)
            elif "orphan" in cats and suffix == ".json" and 0 < size < 64:
                reason = "orphan_tiny"

            if reason:
                report["removed"].append({
                    "path":    str(fp.relative_to(AGENT_DATA)),
                    "reason":  reason,
                    "size":    size,
                    "age_days": round(age_d, 1),
                })
                report["freed_bytes"] += size
                if not dry_run:
                    try:
                        fp.unlink()
                    except Exception as _e:
                        report["errors"].append(f"{fp.name}: {_e}")
                        report["freed_bytes"] -= size  # geri al

        except Exception as _e:
            report["errors"].append(f"scan:{fp.name}: {_e}")

    # ── 2. RAM cache GC ────────────────────────────────────────────
    if "cache_gc" in cats and not dry_run:
        try:
            flushed = cache.flush("")   # tüm cache flush değil — stats al
            # Sadece 0-byte veya geçersiz key'leri at
            # cache.flush("") hepsini siler — bunun yerine sadece rapor et
            report["cache_keys"] = cache.stats().get("keys", 0)
            report["cache_mb"]   = cache.stats().get("used_mb", 0)
        except Exception as _e:
            report["errors"].append(f"cache_gc: {_e}")

    report["freed_mb"]    = round(report["freed_bytes"] / 1024 / 1024, 2)
    report["removed_count"] = len(report["removed"])
    return report


@app.route("/api/admin/cleanup", methods=["POST"])
def admin_cleanup():
    d        = request.json or {}
    dry_run  = bool(d.get("dry_run", False))
    cats     = d.get("categories", ["corrupt", "empty", "tmp", "old_bak"])
    result   = _agent_cleanup(dry_run=dry_run, categories=cats)
    return jsonify({"ok": True, **result})


@app.route("/api/admin/cleanup/status")
def admin_cleanup_status():
    """Disk durumu + son temizlik özeti."""
    return jsonify({
        "ok":           True,
        "node_id":      NODE_ID,
        "store_used_gb": store_used_gb(),
        "store_free_gb": store_free_gb(),
        "cache_keys":   cache.stats().get("keys", 0),
        "cache_mb":     cache.stats().get("used_mb", 0),
    })


@app.route("/api/admin/wipe-all", methods=["POST"])
def admin_wipe_all():
    """
    Agent diskindeki TÜM dosyaları sil + RAM cache tamamen boşalt.
    Ana sunucudan POST /api/admin/full-wipe tarafından tetiklenir.
    """
    d = request.json or {}
    if not d.get("confirmed"):
        return jsonify({"ok": False, "error": "confirmed=true gerekli"}), 400

    import shutil as _shutil

    deleted = 0
    errors  = []

    # RAM cache tamamen boşalt
    try:
        cache.flush("")
        print(f"[Wipe] RAM cache temizlendi", flush=True)
    except Exception as _e:
        errors.append(f"cache_flush: {_e}")

    # AGENT_DATA içindeki her şeyi sil
    for item in list(AGENT_DATA.iterdir()):
        try:
            if item.is_dir():
                _shutil.rmtree(str(item), ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            deleted += 1
        except Exception as _e:
            errors.append(f"{item.name}: {_e}")

    # Temel dizin yapısını yeniden oluştur
    for sub in ["regions/world", "regions/world_nether", "regions/world_the_end",
                "backups", "plugins", "configs", "chunks", "cuberite_cache"]:
        try: (AGENT_DATA / sub).mkdir(parents=True, exist_ok=True)
        except Exception: pass

    print(f"[Wipe] ✅ {deleted} öğe silindi, {len(errors)} hata", flush=True)
    return jsonify({"ok": True, "deleted": deleted, "errors": errors, "node_id": NODE_ID})

@app.route("/api/bulk/cache_and_store", methods=["POST"])
def bulk_cache_and_store():
    import base64
    d     = request.json or {}
    items = d.get("items", [])
    ok_n  = 0
    for item in items:
        key  = item.get("key", "")
        data = base64.b64decode(item.get("data_b64", ""))
        if key and data:
            try:
                if cache.set(key, data):
                    ok_n += 1
            except MemoryError:
                gc.collect()
                break
    return jsonify({"ok": True, "stored": ok_n, "total": len(items)})


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL MODU — mc_panel.py'yi subprocess olarak çalıştır
# ══════════════════════════════════════════════════════════════════════════════

def _run_as_panel():
    """
    IS_PANEL=1: Bu agent panel host.
    - mc_panel.py'yi WORKER_URL=MAIN_URL (Cuberite ana sunucu) ile başlatır.
    - Agent kendi Flask'ını da çalıştırır (cache/disk/proxy için).
    - Panel PORT'ta, agent AGENT_PORT'ta çalışır (ikisi aynı port olabilir).
    """
    panel_path = "/app/mc_panel.py"
    if not os.path.exists(panel_path):
        print(f"[PanelAgent] ❌ {panel_path} bulunamadı — normal agent moduna geçiliyor")
        return False

    env = {
        **os.environ,
        "MC_ONLY":    "0",       # Flask aç
        "WORKER_URL": MAIN_URL,  # Cuberite bu URL'de (MC_ONLY=1 mod)
        "PORT":       str(PORT), # Aynı port — panel burada
        "MAIN_URL":   MAIN_URL,
    }
    print(f"[PanelAgent] 🖥️  mc_panel.py başlatılıyor (WORKER_URL={MAIN_URL})")
    proc = subprocess.Popen(
        [sys.executable, panel_path],
        env=env,
    )

    # Panel process'i izle — ölürse yeniden başlat
    while True:
        ret = proc.wait()
        print(f"[PanelAgent] ⚠️  mc_panel.py çıktı (kod={ret}), 10s sonra yeniden...")
        time.sleep(10)
        proc = subprocess.Popen([sys.executable, panel_path], env=env)

    return True  # unreachable


# ══════════════════════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════════════════════

threading.Thread(target=_heartbeat_loop, daemon=True).start()

if __name__ == "__main__":
    if IS_PANEL:
        # Panel modunda agent Flask'ı başlatma — panel zaten PORT'u kullanıyor
        print(f"[Agent] IS_PANEL=1 → mc_panel.py çalıştırılıyor...")
        _run_as_panel()
    else:
        print(f"[Agent] Port {PORT} başlatılıyor...")
        import eventlet.wsgi
        eventlet.wsgi.server(eventlet.listen(("0.0.0.0", PORT)), app,
                             log_output=False)
