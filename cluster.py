"""
🖥️  VirtualCluster v13.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tüm agent'ların RAM + Disk + CPU'sunu ana sunucuyla birleştirir.
Minecraft sadece yerel bir makine üzerinde çalıştığını sanır.
Kernel modülü gerekmez. Tamamen userspace.

┌─────────────────────────────────────────────────────────────┐
│  SANAL MAKİNE  (MC'nin gördüğü)                             │
│                                                             │
│  RAM  = Ana UserSwap (4GB dosya) + Agent RAM cache'leri     │
│  Disk = Yerel /minecraft + Agent disk store (tier-out)      │
│  CPU  = Şeffaf görev havuzu (auto-distribute)               │
│  Net  = Oyuncu bağlantıları agent proxy'leri üzerinden      │
└─────────────────────────────────────────────────────────────┘

v13.0 Değişiklikleri (v12'ye göre):
  • ClusterMemory.build_swapfile_on_agent() kaldırıldı
    (swapon Render'da EPERM — UserSwap yerel dosyaya yazıyor)
  • Tek agent kaydı: mc_panel._agents + vcluster._agents birleşti
  • Region cache: başlatma öncesi agent'tan region dosyaları geri yükle
  • Sağlık döngüsü: hata sayacı düzeltildi (healthy=False gecikmesi)
  • ClusterNet: proxy watchdog geliştirildi
  • Blueprint route'ları /api/agent/* (mc_panel ile çakışmasın)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, sys, json, time, threading, hashlib, shutil, subprocess, gzip
from pathlib import Path
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Optional
import urllib.request as _ur
import urllib.error

# ── Konfigürasyon ─────────────────────────────────────────────────────────────
MAIN_URL      = "https://wc-tsgd.onrender.com"
MC_DIR        = Path("/minecraft")
PANEL_PORT    = int(os.environ.get("PORT", "5000"))
CLUSTER_MOUNT = Path("/mnt/vcluster")
CACHE_DIR     = Path("/tmp/cluster_cache")
PAPER_CACHE   = MC_DIR / "cache"

for d in [CLUSTER_MOUNT, CACHE_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)
    try:
        _ur.urlopen(_ur.Request(
            f"http://127.0.0.1:{PANEL_PORT}/api/internal/status_msg",
            data=json.dumps({"msg": msg}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=2)
    except Exception:
        pass


# ── HTTP yardımcıları ─────────────────────────────────────────────────────────

def _http(url: str, method: str = "GET", data: bytes = None,
          headers: dict = None, timeout: int = 20) -> Optional[bytes]:
    try:
        req = _ur.Request(
            url, data=data,
            headers={"Content-Type": "application/octet-stream", **(headers or {})},
            method=method,
        )
        with _ur.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _jget(url: str, body: dict = None, method: str = "GET",
          timeout: int = 15) -> Optional[dict]:
    raw = _http(url,
                method=method if body is None else "POST",
                data=json.dumps(body).encode() if body else None,
                headers={"Content-Type": "application/json"},
                timeout=timeout)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


def _stream_download(url: str, dest: Path,
                     timeout: int = 60, chunk: int = 256 * 1024) -> int:
    """URL'den dosyayı stream olarak indir. Başarıyla inen bayt sayısını döner."""
    try:
        req = _ur.Request(url)
        written = 0
        with _ur.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            while True:
                data = r.read(chunk)
                if not data:
                    break
                f.write(data)
                written += len(data)
        return written
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════════════
#  1.  ClusterMemory — Dağıtık RAM Cache
# ══════════════════════════════════════════════════════════════════════════════

class ClusterMemory:
    """
    Agent RAM cache'lerini ana sunucunun uygulama cache'iyle birleştirir.

    NOT: v13'te swapon kaldırıldı.
    Render'da swapon EPERM döner. Bunun yerine:
      • Ana sunucu UserSwap (userswap.so) kullanır → yerel dosyaya yazar
      • Agent'lar MC chunk/entity verilerini RAM cache'inde tutar
      • Cuberite bellek baskısı bu sayede azalır

    Strateji: Distributed application cache
      put(key, data) → en az yüklü agent cache'ine gönder
      get(key)       → yerel hot cache → agent sırasıyla ara
    """

    def __init__(self, agents: dict):
        self._agents     = agents
        self._cache_hits = 0
        self._cache_miss = 0

        # Yerel hot cache (64MB) — en sık erişilen veriler burada
        self._local:      OrderedDict[str, bytes] = OrderedDict()
        self._local_max   = 64 * 1024 * 1024
        self._local_size  = 0
        self._local_lock  = threading.Lock()

    # ── Cache erişimi ────────────────────────────────────────────────────────

    def put(self, key: str, data: bytes) -> bool:
        """Veriyi önce yerel hot cache'e, taşarsa en az yüklü agent'a yaz."""
        with self._local_lock:
            if len(data) + self._local_size <= self._local_max:
                if key in self._local:
                    self._local_size -= len(self._local[key])
                self._local[key] = data
                self._local_size += len(data)
                self._local.move_to_end(key)
                return True
        return self._put_remote(key, data)

    def get(self, key: str) -> Optional[bytes]:
        # Yerel hot cache
        with self._local_lock:
            if key in self._local:
                self._local.move_to_end(key)
                self._cache_hits += 1
                return self._local[key]
        # Agent cache'leri
        for ag in self._healthy():
            raw = _http(f"{ag['url']}/api/cache/get/{key}", timeout=8)
            if raw:
                with self._local_lock:
                    self._local[key] = raw
                    self._local_size += len(raw)
                    self._evict_local()
                self._cache_hits += 1
                return raw
        self._cache_miss += 1
        return None

    def delete(self, key: str):
        with self._local_lock:
            if key in self._local:
                self._local_size -= len(self._local[key])
                del self._local[key]
        for ag in self._healthy():
            _http(f"{ag['url']}/api/cache/delete/{key}", method="DELETE", timeout=5)

    def flush(self) -> int:
        with self._local_lock:
            n = len(self._local)
            self._local.clear()
            self._local_size = 0
        for ag in self._healthy():
            _jget(f"{ag['url']}/api/cache/flush", {}, timeout=10)
        return n

    # ── Region cache (Cuberite .mca dosyaları) ──────────────────────────────

    def save_remap_cache(self) -> int:
        """
        Remap tamamlandı → cache/patched_*.jar dosyalarını tüm agent'lara yedekle.
        Sonraki başlatmada restore_remap_cache() ile geri yüklenir → remap atlanır.
        """
        PAPER_CACHE.mkdir(parents=True, exist_ok=True)
        files = list(PAPER_CACHE.glob("patched_*.jar")) + \
                list(PAPER_CACHE.glob("*.jar"))
        if not files:
            return 0

        agents = self._healthy()
        if not agents:
            return 0

        import concurrent.futures as _cf
        saved = 0
        for f in files:
            try:
                data  = f.read_bytes()
                fname = f.name

                def _up(a, _d=data, _n=fname):
                    try:
                        return _http(
                            f"{a['url']}/api/files/paper_cache/{_n}",
                            method="PUT", data=_d, timeout=120
                        ) is not None
                    except Exception:
                        return False

                with _cf.ThreadPoolExecutor(max_workers=len(agents)) as ex:
                    results = list(ex.map(_up, agents))
                n = sum(1 for r in results if r)
                if n:
                    _log(f"[Cluster] 💾 Remap cache yedek: {fname} "
                         f"({len(data)//1024//1024}MB) → {n}/{len(agents)} agent")
                    saved += 1
                del data
            except Exception as e:
                _log(f"[Cluster] ⚠️  Remap cache yedek hatası: {e}")
        return saved

    def restore_remap_cache(self) -> bool:
        """
        MC başlamadan önce agent'lardan remap cache'i geri yükle.
        Başarılı olursa Cuberite disk I/O azalır → başlatma hızlanır.
        """
        agents = self._healthy()
        if not agents:
            return False

        PAPER_CACHE.mkdir(parents=True, exist_ok=True)
        restored = 0

        for ag in agents:
            try:
                r = _jget(f"{ag['url']}/api/files/paper_cache", timeout=10)
                for fi in (r or {}).get("files", []):
                    fname = fi.get("name", "") if isinstance(fi, dict) else str(fi)
                    if not fname.endswith(".jar"):
                        continue
                    dest = PAPER_CACHE / fname
                    if dest.exists() and dest.stat().st_size > 1024 * 1024:
                        continue  # Zaten var
                    n = _stream_download(
                        f"{ag['url']}/api/files/paper_cache/{fname}",
                        dest, timeout=120
                    )
                    if n > 1024 * 1024:
                        _log(f"[Cluster] ✅ Remap cache geri yüklendi: "
                             f"{fname} ({n//1024//1024}MB)")
                        restored += 1
                        break  # Bir agenten aldık yeter
            except Exception:
                continue

        if restored:
            _log(f"[Cluster] 🚀 Remap cache hazır ({restored} dosya) — remapping ATLANABİLİR")
        return restored > 0

    # ── UserSwap istatistikleri ──────────────────────────────────────────────

    def userswap_stats(self) -> dict:
        """userswap.so istatistiklerini oku (/tmp/userswap.stats JSON)."""
        try:
            import json as _j
            return _j.loads(open("/tmp/userswap.stats").read())
        except Exception:
            return {}

    # ── İstatistik ──────────────────────────────────────────────────────────

    def stats(self) -> dict:
        total = self._cache_hits + self._cache_miss
        us    = self.userswap_stats()
        return {
            "local_cache_mb":    round(self._local_size / 1024 / 1024, 1),
            "local_cache_keys":  len(self._local),
            "hit_rate":          round(self._cache_hits / total * 100, 1) if total else 0,
            "userswap":          us,
            "userswap_used_mb":  us.get("used_mb", 0),
            "userswap_total_mb": us.get("total_mb", 0),
        }

    # ── Yardımcılar ─────────────────────────────────────────────────────────

    def _healthy(self) -> list:
        return [a for a in self._agents.values() if a.get("healthy")]

    def _put_remote(self, key: str, data: bytes) -> bool:
        ag = self._least_loaded()
        if not ag:
            return False
        r = _http(f"{ag['url']}/api/cache/set?key={key}",
                  method="POST", data=data, timeout=15)
        return r is not None

    def _evict_local(self):
        while self._local_size > self._local_max and self._local:
            k, v = self._local.popitem(last=False)
            self._local_size -= len(v)

    def _least_loaded(self) -> Optional[dict]:
        h = self._healthy()
        if not h:
            return None
        return min(h, key=lambda a: a.get("info", {}).get("ram", {}).get("used_mb", 9999))


# ══════════════════════════════════════════════════════════════════════════════
#  2.  ClusterDisk — Disk Birleştirme
# ══════════════════════════════════════════════════════════════════════════════

class ClusterDisk:
    """
    Yerel /minecraft + agent disk store tek sanal disk gibi davranır.

    Tier-out: disk dolunca eski region'lar agent'a taşınır.
    Fetch: MC erişmeden önce agent'tan geri indirilir.
    """

    def __init__(self, agents: dict):
        self._agents     = agents
        self._file_index: dict[str, str] = {}  # filename → agent node_id
        self._index_lock = threading.Lock()
        threading.Thread(target=self._tier_loop, daemon=True).start()

    def rebuild_index(self):
        """Tüm agent'lardaki dosyaları listele, index oluştur."""
        new_index = {}
        for nid, ag in self._agents.items():
            if not ag.get("healthy"):
                continue
            for cat in ["regions", "chunks"]:
                r = _jget(f"{ag['url']}/api/files/{cat}", timeout=10)
                for fi in (r or {}).get("files", []):
                    fname = fi.get("name", "")
                    if fname:
                        new_index[fname] = nid
        with self._index_lock:
            self._file_index = new_index

    def fetch_region(self, dimension: str, filename: str) -> bool:
        """Region dosyasını agent'tan indir."""
        with self._index_lock:
            nid = self._file_index.get(filename)
        if not nid or nid not in self._agents:
            return False
        ag   = self._agents[nid]
        dest = MC_DIR / dimension / "region" / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = _stream_download(
            f"{ag['url']}/api/files/regions/{dimension}/{filename}",
            dest, timeout=60
        )
        if n > 0:
            _log(f"[Disk] ⬇  {filename} agent'tan getirildi ({n//1024}KB)")
            return True
        return False

    def tier_out(self, older_than_days: int = 7) -> int:
        """Eski region dosyalarını agent'a taşı, disk açılsın."""
        best = self._best_disk_agent()
        if not best:
            return 0

        now        = time.time()
        freed      = 0
        dim_paths  = [
            (MC_DIR / "world" / "region",                     "world"),
            (MC_DIR / "world_nether" / "DIM-1" / "region",   "world_nether"),
            (MC_DIR / "world_the_end" / "DIM1" / "region",   "world_the_end"),
        ]

        for dim_dir, dim in dim_paths:
            if not dim_dir.exists():
                continue
            for rf in sorted(dim_dir.glob("*.mca"), key=lambda f: f.stat().st_mtime):
                age_days = (now - rf.stat().st_mtime) / 86400
                if age_days < older_than_days:
                    continue
                try:
                    data = rf.read_bytes()
                    r = _http(
                        f"{best['url']}/api/files/regions/{dim}/{rf.name}",
                        method="PUT", data=data, timeout=120
                    )
                    if r is not None:
                        rf.unlink()
                        with self._index_lock:
                            self._file_index[rf.name] = best["node_id"]
                        freed += len(data)
                    del data
                except Exception:
                    continue

        if freed:
            _log(f"[Disk] 💾 Tier-out: {freed//1_000_000:.0f}MB agent'a taşındı")
        return freed

    def _tier_loop(self):
        while True:
            time.sleep(120)
            try:
                free_gb = shutil.disk_usage("/").free / 1e9
                if free_gb < 8.0:
                    days = 7 if free_gb > 4 else 2
                    self.tier_out(older_than_days=days)
            except Exception:
                pass

    def stats(self) -> dict:
        dk         = shutil.disk_usage("/")
        with self._index_lock:
            remote_files = len(self._file_index)
        remote_gb  = sum(
            a.get("info", {}).get("disk", {}).get("store_used_gb", 0)
            for a in self._agents.values() if a.get("healthy")
        )
        return {
            "local_free_gb":  round(dk.free  / 1e9, 2),
            "local_total_gb": round(dk.total / 1e9, 2),
            "remote_files":   remote_files,
            "remote_gb":      round(remote_gb, 2),
            "total_gb":       round(dk.total / 1e9 + remote_gb, 2),
        }

    def _healthy(self) -> list:
        return [a for a in self._agents.values() if a.get("healthy")]

    def _best_disk_agent(self) -> Optional[dict]:
        h = self._healthy()
        return max(h,
                   key=lambda a: a.get("info", {}).get("disk", {}).get("store_free_gb", 0),
                   default=None)


# ══════════════════════════════════════════════════════════════════════════════
#  3.  ClusterCPU — CPU Birleştirme
# ══════════════════════════════════════════════════════════════════════════════

class ClusterCPU:
    """
    Ana sunucu + agent CPU'ları tek havuzda.
    Yoğun işlemleri (sıkıştırma, hash) agent'lara dağıt, yerel CPU'yu boşalt.
    """

    def __init__(self, agents: dict):
        self._agents  = agents
        self._local_q = __import__("queue").Queue()
        self._results: dict[str, dict] = {}
        self._lock    = threading.Lock()
        for _ in range(2):
            threading.Thread(target=self._local_worker, daemon=True).start()

    def submit(self, task_type: str, payload: dict,
               prefer_remote: bool = True) -> str:
        tid = hashlib.md5(
            f"{task_type}{payload}{time.time()}".encode()
        ).hexdigest()[:12]
        with self._lock:
            self._results[tid] = {"status": "pending"}

        if prefer_remote:
            threading.Thread(
                target=self._remote_submit,
                args=(tid, task_type, payload),
                daemon=True
            ).start()
        else:
            self._local_q.put((tid, task_type, payload))
        return tid

    def wait(self, tid: str, timeout: int = 30) -> Optional[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                r = self._results.get(tid, {})
            if r.get("status") == "done":
                return r.get("result")
            if r.get("status") == "error":
                return None
            time.sleep(0.3)
        return None

    def run(self, task_type: str, payload: dict, timeout: int = 30):
        return self.wait(self.submit(task_type, payload), timeout)

    def _remote_submit(self, tid, task_type, payload):
        ag = self._least_cpu_agent()
        if ag:
            r = _jget(f"{ag['url']}/api/cpu/submit",
                      {"type": task_type, "payload": payload}, timeout=10)
            if r and r.get("ok"):
                remote_tid = r.get("task_id")
                deadline   = time.time() + 60
                while time.time() < deadline:
                    time.sleep(1)
                    res = _jget(f"{ag['url']}/api/cpu/result/{remote_tid}", timeout=8)
                    if res and res.get("status") == "done":
                        with self._lock:
                            self._results[tid] = {"status": "done",
                                                   "result": res.get("result")}
                        return
        self._local_q.put((tid, task_type, payload))

    def _local_worker(self):
        while True:
            tid, task_type, payload = self._local_q.get()
            try:
                result = self._exec_local(task_type, payload)
                with self._lock:
                    self._results[tid] = {"status": "done", "result": result}
            except Exception as e:
                with self._lock:
                    self._results[tid] = {"status": "error", "error": str(e)}

    def _exec_local(self, task_type, payload):
        if task_type == "compress_file":
            src  = Path(payload["path"])
            dest = Path(payload.get("dest", str(src) + ".gz"))
            with open(src, "rb") as f, gzip.open(dest, "wb", compresslevel=6) as g:
                shutil.copyfileobj(f, g)
            return {"dest": str(dest), "size": dest.stat().st_size}
        elif task_type == "hash_files":
            root = Path(payload.get("path", str(MC_DIR)))
            out  = {}
            for f in root.rglob(payload.get("pattern", "*.mca")):
                out[f.name] = hashlib.md5(f.read_bytes()).hexdigest()
            return out
        elif task_type == "disk_usage":
            dk = shutil.disk_usage(payload.get("path", "/"))
            return {"free_gb": round(dk.free / 1e9, 2), "total_gb": round(dk.total / 1e9, 2)}
        elif task_type == "echo":
            return payload
        return {"error": f"Bilinmeyen: {task_type}"}

    def stats(self) -> dict:
        pending = sum(1 for r in self._results.values() if r.get("status") == "pending")
        healthy = [a for a in self._agents.values() if a.get("healthy")]
        remote_cores = sum(a.get("info", {}).get("cpu", {}).get("cores", 0) for a in healthy)
        return {
            "local_cores":  os.cpu_count() or 1,
            "remote_cores": remote_cores,
            "total_cores":  (os.cpu_count() or 1) + remote_cores,
            "tasks_pending": pending,
        }

    def _least_cpu_agent(self) -> Optional[dict]:
        h = [a for a in self._agents.values() if a.get("healthy")]
        return min(h, key=lambda a: a.get("info", {}).get("cpu", {}).get("load1", 99),
                   default=None)


# ══════════════════════════════════════════════════════════════════════════════
#  4.  ClusterNet — Ağ + Proxy
# ══════════════════════════════════════════════════════════════════════════════

class ClusterNet:
    """
    MC açıkken tüm agent'larda proxy başlat.
    MC kapanınca proxy'leri durdur.
    """

    def __init__(self, agents: dict):
        self._agents         = agents
        self._active_proxies: set[str] = set()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def start_all(self, mc_host: str = "127.0.0.1", mc_port: int = 25565) -> list:
        started = []
        for nid, ag in self._agents.items():
            if not ag.get("healthy"):
                continue
            r = _jget(f"{ag['url']}/api/proxy/start",
                      {"host": mc_host, "port": mc_port, "listen_port": 25565},
                      timeout=10)
            if r and r.get("ok"):
                self._active_proxies.add(nid)
                started.append(nid)
        if started:
            _log(f"[Net] 🔀 Proxy aktif: {len(started)} agent")
        return started

    def stop_all(self):
        for nid in list(self._active_proxies):
            ag = self._agents.get(nid)
            if ag:
                _jget(f"{ag['url']}/api/proxy/stop", {}, timeout=5)
            self._active_proxies.discard(nid)

    def _watchdog(self):
        """MC açık/kapalı olduğunda proxy'leri otomatik yönet."""
        mc_up_prev = False
        while True:
            time.sleep(12)
            mc_up = False
            try:
                import socket as _sock
                s = _sock.create_connection(("127.0.0.1", 25565), 1)
                s.close()
                mc_up = True
            except Exception:
                pass

            if mc_up and not mc_up_prev:
                self.start_all()
            elif not mc_up and mc_up_prev:
                self.stop_all()
            mc_up_prev = mc_up

    def stats(self) -> dict:
        conns = sum(
            self._agents.get(nid, {}).get("info", {}).get("proxy", {}).get("connections", 0)
            for nid in self._active_proxies
        )
        return {
            "active_proxies":    len(self._active_proxies),
            "total_connections": conns,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  5.  VirtualCluster — Ana Orkestratör
# ══════════════════════════════════════════════════════════════════════════════

class VirtualCluster:
    """
    Tek agent kaydı (mc_panel._agents ile birleştirildi).
    vcluster._agents → tek kaynak.

    Kullanım (mc_panel.py):
        from cluster import vcluster, cluster_api
        app.register_blueprint(cluster_api)
        vcluster.set_socketio(socketio)

    Agent kaydı:
        vcluster.register_agent(tunnel, node_id, info)
        vcluster.heartbeat_agent(node_id, info)

    Özellikler:
        vcluster.memory  → ClusterMemory
        vcluster.disk    → ClusterDisk
        vcluster.cpu     → ClusterCPU
        vcluster.net     → ClusterNet
        vcluster.summary → panel için dict
    """

    def __init__(self):
        self._agents: dict[str, dict] = {}
        self._lock   = threading.Lock()
        self._sio    = None

        self.memory  = ClusterMemory(self._agents)
        self.disk    = ClusterDisk(self._agents)
        self.cpu     = ClusterCPU(self._agents)
        self.net     = ClusterNet(self._agents)

        threading.Thread(target=self._health_loop, daemon=True).start()
        _log("[Cluster] ✅ VirtualCluster v13.0 hazır")

    # ── Agent yönetimi ───────────────────────────────────────────────────────

    def register_agent(self, tunnel: str, node_id: str, info: dict):
        """Agent'ı kaydet veya güncelle."""
        tunnel = tunnel.rstrip("/")
        with self._lock:
            is_new = node_id not in self._agents
            self._agents[node_id] = {
                "url":          tunnel,
                "node_id":      node_id,
                "healthy":      True,
                "fail_count":   0,
                "last_ping":    time.time(),
                "connected_at": self._agents.get(node_id, {}).get(
                    "connected_at", time.time()),
                "info":         info,
            }
        if is_new:
            _log(f"[Cluster] 🔗 Yeni agent: {node_id} | "
                 f"RAM free:{info.get('ram',{}).get('free_mb',0)}MB | "
                 f"Disk free:{info.get('disk',{}).get('store_free_gb',0):.1f}GB")
            threading.Thread(target=self._onboard, args=(node_id,), daemon=True).start()
        self._emit()

    def heartbeat_agent(self, node_id: str, info: dict):
        """Heartbeat ile agent bilgisini güncelle."""
        with self._lock:
            if node_id in self._agents:
                self._agents[node_id]["info"]       = info
                self._agents[node_id]["healthy"]    = True
                self._agents[node_id]["fail_count"] = 0
                self._agents[node_id]["last_ping"]  = time.time()
        self._emit()

    def get_agents(self) -> list:
        with self._lock:
            return list(self._agents.values())

    def agent_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._agents.values() if a.get("healthy"))

    # ── Onboarding ───────────────────────────────────────────────────────────

    def _onboard(self, node_id: str):
        """Yeni agent bağlandığında: disk index yenile."""
        time.sleep(3)
        self.disk.rebuild_index()
        self._emit()

    # ── Sağlık döngüsü ──────────────────────────────────────────────────────

    def _health_loop(self):
        while True:
            time.sleep(25)
            with self._lock:
                agents = list(self._agents.values())
            changed = False
            for ag in agents:
                r = _jget(f"{ag['url']}/api/status", timeout=10)
                if r:
                    ag["info"]       = r
                    ag["healthy"]    = True
                    ag["fail_count"] = 0
                    ag["last_ping"]  = time.time()
                    changed = True
                else:
                    ag["fail_count"] = ag.get("fail_count", 0) + 1
                    if ag["fail_count"] >= 3 and ag.get("healthy", True):
                        ag["healthy"] = False
                        _log(f"[Cluster] ⚠️  {ag['node_id']} erişilemiyor "
                             f"({ag['fail_count']} fail)")
                        changed = True
            if changed:
                self._emit()

    # ── Panel özeti ──────────────────────────────────────────────────────────

    def summary(self) -> dict:
        with self._lock:
            agents = list(self._agents.values())

        healthy    = [a for a in agents if a.get("healthy")]
        mem_stat   = self.memory.stats()
        disk_stat  = self.disk.stats()
        cpu_stat   = self.cpu.stats()
        net_stat   = self.net.stats()

        total_agent_ram_mb  = sum(a.get("info", {}).get("ram", {}).get("free_mb", 0) for a in healthy)
        total_agent_disk_gb = sum(a.get("info", {}).get("disk", {}).get("store_free_gb", 0) for a in healthy)

        return {
            "total":   len(agents),
            "healthy": len(healthy),
            "virtual_machine": {
                "agent_cache_mb":    total_agent_ram_mb,
                "local_cache_mb":    mem_stat["local_cache_mb"],
                "userswap_used_mb":  mem_stat.get("userswap_used_mb", 0),
                "userswap_total_mb": mem_stat.get("userswap_total_mb", 0),
                "total_disk_gb":     disk_stat["total_gb"],
                "remote_disk_gb":    disk_stat["remote_gb"],
                "total_cpu_cores":   cpu_stat["total_cores"],
                "active_proxies":    net_stat["active_proxies"],
            },
            "memory":  mem_stat,
            "disk":    disk_stat,
            "cpu":     cpu_stat,
            "net":     net_stat,
            "agents": [
                {
                    "node_id":      a["node_id"],
                    "url":          a["url"],
                    "healthy":      a["healthy"],
                    "connected_at": a.get("connected_at", 0),
                    "last_ping":    a.get("last_ping", 0),
                    "fail_count":   a.get("fail_count", 0),
                    "ram":          a.get("info", {}).get("ram",   {}),
                    "disk":         a.get("info", {}).get("disk",  {}),
                    "cpu":          a.get("info", {}).get("cpu",   {}),
                    "proxy":        a.get("info", {}).get("proxy", {}),
                    "cache":        a.get("info", {}).get("cache", {}),
                }
                for a in agents
            ],
        }

    def set_socketio(self, sio):
        self._sio = sio

    def _emit(self):
        if self._sio:
            try:
                self._sio.emit("cluster_update", self.summary())
            except Exception:
                pass


# ── Singleton ─────────────────────────────────────────────────────────────────
vcluster = VirtualCluster()


# ── Flask Blueprint ───────────────────────────────────────────────────────────
try:
    from flask import Blueprint, request, jsonify, Response
    cluster_api = Blueprint("cluster_api", __name__)

    # NOT: /api/agent/register ve /api/agent/heartbeat route'ları
    # mc_panel.py'de tanımlı — burada tanımlamak Flask route çakışmasına yol açar.
    # mc_panel.py o route'larda vcluster.register_agent() + vcluster.heartbeat_agent() çağırır.

    @cluster_api.route("/api/cluster/status")
    def c_status():
        return jsonify(vcluster.summary())

    @cluster_api.route("/api/cluster/cache/get/<path:key>")
    def c_cache_get(key):
        v = vcluster.memory.get(key)
        if v is None:
            return jsonify({"ok": False}), 404
        return Response(v, mimetype="application/octet-stream")

    @cluster_api.route("/api/cluster/cache/put", methods=["POST"])
    def c_cache_put():
        key  = request.args.get("key", "")
        data = request.get_data()
        ok   = vcluster.memory.put(key, data) if key else False
        return jsonify({"ok": ok})

    @cluster_api.route("/api/cluster/cache/flush", methods=["POST"])
    def c_cache_flush():
        n = vcluster.memory.flush()
        return jsonify({"ok": True, "flushed": n})

    @cluster_api.route("/api/cluster/disk/fetch", methods=["POST"])
    def c_disk_fetch():
        d  = request.json or {}
        ok = vcluster.disk.fetch_region(d.get("dim", "world"), d.get("file", ""))
        return jsonify({"ok": ok})

    @cluster_api.route("/api/cluster/disk/tier_out", methods=["POST"])
    def c_tier_out():
        days = int((request.json or {}).get("days", 7))
        freed = vcluster.disk.tier_out(older_than_days=days)
        return jsonify({"ok": True, "freed_bytes": freed})

    @cluster_api.route("/api/cluster/disk/rebuild_index", methods=["POST"])
    def c_rebuild_index():
        vcluster.disk.rebuild_index()
        return jsonify({"ok": True})

    @cluster_api.route("/api/cluster/cpu/run", methods=["POST"])
    def c_cpu_run():
        d   = request.json or {}
        res = vcluster.cpu.run(d.get("type", "echo"), d.get("payload", {}))
        return jsonify({"ok": True, "result": res})

    @cluster_api.route("/api/cluster/net/proxies/start", methods=["POST"])
    def c_proxy_start():
        d = request.json or {}
        started = vcluster.net.start_all(
            d.get("host", "127.0.0.1"), int(d.get("port", 25565))
        )
        return jsonify({"ok": True, "started": started})

    @cluster_api.route("/api/cluster/net/proxies/stop", methods=["POST"])
    def c_proxy_stop():
        vcluster.net.stop_all()
        return jsonify({"ok": True})

    @cluster_api.route("/api/cluster/remap_cache/restore", methods=["POST"])
    def c_remap_restore():
        ok = vcluster.memory.restore_remap_cache()
        return jsonify({"ok": ok})

    @cluster_api.route("/api/cluster/remap_cache/save", methods=["POST"])
    def c_remap_save():
        n = vcluster.memory.save_remap_cache()
        return jsonify({"ok": True, "saved": n})

    @cluster_api.route("/api/pool/status")  # mc_panel uyumluluk route'u
    def c_pool_compat():
        return jsonify(vcluster.summary())

    @cluster_api.route("/api/ping")
    def c_ping():
        return jsonify({"ok": True})

except ImportError:
    cluster_api = None
