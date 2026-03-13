"""
🔗  Resource Pool Manager — v11.0  (Her zaman aktif)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MC sunucusu açık/kapalı fark etmez.
Bu modül şunları yapar:

  ✅ Agent'lardan gelen kaynakları sürekli izler
  ✅ Disk dolmaya başlayınca otomatik region arşivler → swap büyütür
  ✅ RAM azalınca otomatik cache offload başlatır
  ✅ Her agent'ı 20sn'de bir health check yapar
  ✅ Pool durumunu mc_panel.py'e SocketIO ile bildirir

mc_panel.py'de kullanımı:
    from resource_pool import pool, pool_api   # pool_api = Blueprint
"""

import os, sys, json, time, threading, hashlib, shutil, subprocess
from pathlib import Path
from datetime import datetime
import urllib.request as _ur
import urllib.error

# mc_panel.py'den import edildiğinde Flask uyumlu olması için
try:
    from flask import Blueprint, request, jsonify
    _has_flask = True
except ImportError:
    _has_flask = False

# ─── Konfigürasyon ─────────────────────────────────────────────
MC_DIR     = Path(os.environ.get("MC_DIR", "/minecraft"))
PANEL_PORT = int(os.environ.get("PORT", "5000"))

# Disk eşikleri (GB)
DISK_WARN_GB  = 7.0    # Bu değerin altına düşünce arşivlemeye başla
DISK_CRIT_GB  = 4.0    # Kritik eşik — daha agresif arşivle
RAM_WARN_MB   = 300    # Boş RAM bu değerin altına düşünce cache offload

# Ne kadar eski region arşivlensin
ARCHIVE_AGE_DAYS_NORMAL   = 7
ARCHIVE_AGE_DAYS_CRITICAL = 2

# ──────────────────────────────────────────────────────────────


class ResourcePool:
    """
    Ana sınıf. mc_panel.py import eder: `from resource_pool import pool`
    """

    def __init__(self):
        self._agents: dict[str, dict] = {}   # node_id → agent_dict
        self._lock = threading.Lock()
        self._socketio = None   # mc_panel başlattığında inject eder

        # Arka plan işleri
        threading.Thread(target=self._health_loop,     daemon=True).start()
        threading.Thread(target=self._optimize_loop,   daemon=True).start()
        threading.Thread(target=self._auto_proxy_loop, daemon=True).start()

        print("[Pool] ✅ ResourcePool v11.0 başlatıldı (sürekli aktif)")

    # ──────────────────────────────────────────────────────────
    #  Agent Kayıt / Heartbeat
    # ──────────────────────────────────────────────────────────

    def register(self, tunnel_url: str, node_id: str, info: dict) -> bool:
        with self._lock:
            is_new = node_id not in self._agents
            self._agents[node_id] = {
                "url":          tunnel_url.rstrip("/"),
                "node_id":      node_id,
                "healthy":      True,
                "fail_count":   0,
                "last_ping":    time.time(),
                "connected_at": self._agents.get(node_id, {}).get("connected_at", time.time()),
                "info":         info,
            }
        if is_new:
            self._log(f"[Pool] ✅ Yeni agent: {node_id} | "
                      f"RAM:{info.get('ram',{}).get('free_mb',0)}MB | "
                      f"Disk:{info.get('disk',{}).get('free_gb',0):.1f}GB")
        self._emit_update()
        return is_new

    # ──────────────────────────────────────────────────────────
    #  Sağlık İzleme  (her 20sn)
    # ──────────────────────────────────────────────────────────

    def _health_loop(self):
        while True:
            time.sleep(20)
            with self._lock:
                agents = list(self._agents.values())
            for ag in agents:
                r = self._json(ag, "GET", "/api/status", timeout=10)
                if r:
                    ag["info"]       = r
                    ag["healthy"]    = True
                    ag["fail_count"] = 0
                    ag["last_ping"]  = time.time()
                else:
                    ag["fail_count"] = ag.get("fail_count", 0) + 1
                    if ag["fail_count"] >= 3:
                        if ag["healthy"]:
                            self._log(f"[Pool] ⚠️  {ag['node_id']} erişilemiyor ({ag['fail_count']} deneme)")
                        ag["healthy"] = False
                    # 3 dakika yanıt yoksa tekrar kayıt denemesi için healthy=True yap
                    # (Agent kendi heartbeat'ini atar, zaten yeniden register eder)
            self._emit_update()

    # ──────────────────────────────────────────────────────────
    #  Otomatik Optimizasyon  (her 5 dakika)
    #  MC açık/kapalı fark etmez
    # ──────────────────────────────────────────────────────────

    def _optimize_loop(self):
        time.sleep(60)   # Başlangıçta 1 dakika bekle
        while True:
            try:
                self._check_disk_and_archive()
                self._check_ram_and_offload()
            except Exception as e:
                print(f"[Pool] ❌ optimize_loop hata: {e}")
            time.sleep(300)   # 5 dakika

    def _check_disk_and_archive(self):
        """Ana sunucunun diski doluysa region'ları agent'a taşı → swap büyüt."""
        if not MC_DIR.exists():
            return
        disk      = shutil.disk_usage("/")
        free_gb   = disk.free / 1e9
        best      = self._best_by_disk()

        if free_gb < DISK_WARN_GB and best:
            age_days = ARCHIVE_AGE_DAYS_CRITICAL if free_gb < DISK_CRIT_GB else ARCHIVE_AGE_DAYS_NORMAL
            self._log(f"[Pool] 💾 Disk düşük ({free_gb:.1f}GB boş) — {age_days}+ günlük regionlar arşivleniyor...")
            archived, freed_mb = self._archive_regions(best, age_days)
            if archived > 0:
                self._log(f"[Pool] ✅ {archived} region arşivlendi ({freed_mb:.0f}MB boşaltıldı)")
                self._expand_swap()

    def _check_ram_and_offload(self):
        """Ana sunucu RAM'i azaldıysa geçici verileri cache agent'a yükle."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            if mem.available / 1024 / 1024 < RAM_WARN_MB:
                # Belleği rahatlatmak için GC tetikle
                subprocess.run("kill -s SIGUSR1 $(pgrep java) 2>/dev/null", shell=True)
        except: pass

    def _archive_regions(self, agent: dict, older_than_days: int) -> tuple[int, float]:
        now      = time.time()
        archived = 0
        freed_mb = 0.0

        dims = {
            "world":       MC_DIR / "world"       / "region",
            "world_nether": MC_DIR / "world_nether" / "DIM-1" / "region",
            "world_the_end": MC_DIR / "world_the_end" / "DIM1" / "region",
        }
        for dim, region_dir in dims.items():
            if not region_dir.exists():
                continue
            for rf in sorted(region_dir.glob("*.mca"),
                             key=lambda f: f.stat().st_mtime):
                if (now - rf.stat().st_mtime) / 86400 < older_than_days:
                    continue
                try:
                    data = rf.read_bytes()
                    url  = agent["url"] + f"/api/files/regions/{dim}/{rf.name}"
                    req  = _ur.Request(url, data=data, method="PUT",
                                       headers={"Content-Type": "application/octet-stream"})
                    _ur.urlopen(req, timeout=120)
                    rf.unlink()
                    freed_mb += len(data) / 1e6
                    archived  += 1
                except Exception as e:
                    print(f"[Pool] ⚠️  Arşiv hatası {rf.name}: {e}")
        return archived, freed_mb

    def _expand_swap(self):
        """Boşalan disk ile swap dosyasını büyüt."""
        disk     = shutil.disk_usage("/")
        free_gb  = disk.free / 1e9
        sw_mb    = min(6144, int(free_gb * 0.7 * 1024))
        if sw_mb < 256:
            return
        sf2 = "/swapfile2"
        subprocess.run(f"swapoff {sf2} 2>/dev/null", shell=True)
        try:
            if Path(sf2).exists(): Path(sf2).unlink()
        except: pass
        r = subprocess.run(
            f"fallocate -l {sw_mb}M {sf2} && chmod 600 {sf2} && "
            f"mkswap -f {sf2} && swapon -p 1 {sf2}",
            shell=True, capture_output=True
        )
        if r.returncode == 0:
            self._log(f"[Pool] 💾 Swap genişletildi: {sw_mb}MB (/swapfile2)")

    # ──────────────────────────────────────────────────────────
    #  Otomatik Proxy  — MC başladığında her agent'ta proxy aç
    # ──────────────────────────────────────────────────────────

    def _auto_proxy_loop(self):
        """MC port'u açıldıysa tüm sağlıklı agent'larda proxy başlat."""
        import socket as _sock
        proxy_started = set()
        while True:
            time.sleep(15)
            # MC çalışıyor mu?
            mc_up = False
            try:
                s = _sock.create_connection(("127.0.0.1", 25565), 1)
                s.close()
                mc_up = True
            except: pass

            with self._lock:
                agents = [a for a in self._agents.values() if a["healthy"]]

            for ag in agents:
                nid = ag["node_id"]
                if mc_up and nid not in proxy_started:
                    r = self._json(ag, "POST", "/api/proxy/start",
                                   {"host": "127.0.0.1", "port": 25565, "listen_port": 25565})
                    if r and r.get("ok"):
                        proxy_started.add(nid)
                        self._log(f"[Pool] 🔀 Proxy başlatıldı: {nid}")
                elif not mc_up and nid in proxy_started:
                    self._json(ag, "POST", "/api/proxy/stop")
                    proxy_started.discard(nid)

    # ──────────────────────────────────────────────────────────
    #  Agent Seçimi
    # ──────────────────────────────────────────────────────────

    def _healthy(self) -> list:
        with self._lock:
            return [a for a in self._agents.values() if a["healthy"]]

    def _best_by_disk(self) -> dict | None:
        h = self._healthy()
        return max(h, key=lambda a: a["info"].get("disk", {}).get("free_gb", 0), default=None)

    def _best_by_ram(self) -> dict | None:
        h = self._healthy()
        return min(h, key=lambda a: a["info"].get("ram", {}).get("cache_mb", 9999), default=None)

    def _round_robin(self, key: str = "") -> dict | None:
        h = self._healthy()
        if not h: return None
        idx = int(hashlib.md5((key or str(time.time())).encode()).hexdigest(), 16) % len(h)
        return h[idx]

    # ──────────────────────────────────────────────────────────
    #  HTTP Yardımcıları
    # ──────────────────────────────────────────────────────────

    def _req(self, agent: dict, method: str, path: str,
             data: bytes = None, headers: dict = None, timeout: int = 15) -> bytes | None:
        try:
            req = _ur.Request(
                agent["url"] + path,
                data=data,
                headers={"Content-Type": "application/octet-stream", **(headers or {})},
                method=method,
            )
            with _ur.urlopen(req, timeout=timeout) as r:
                agent["healthy"]   = True
                agent["last_ping"] = time.time()
                return r.read()
        except Exception:
            agent["fail_count"] = agent.get("fail_count", 0) + 1
            if agent["fail_count"] >= 3:
                agent["healthy"] = False
            return None

    def _json(self, agent: dict, method: str, path: str,
              body: dict = None, timeout: int = 15) -> dict | None:
        raw = self._req(
            agent, method, path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if raw:
            try: return json.loads(raw)
            except: pass
        return None

    # ──────────────────────────────────────────────────────────
    #  Public API  (mc_panel.py'den çağrılır)
    # ──────────────────────────────────────────────────────────

    def summary(self) -> dict:
        with self._lock:
            agents = list(self._agents.values())
        healthy = [a for a in agents if a["healthy"]]
        res = {
            "ram_free_mb":   sum(a["info"].get("ram",  {}).get("free_mb",  0) for a in healthy),
            "disk_free_gb":  round(sum(a["info"].get("disk", {}).get("free_gb",  0) for a in healthy), 1),
            "cache_used_mb": round(sum(a["info"].get("ram",  {}).get("cache_mb", 0) for a in healthy), 1),
            "cpu_cores":     sum(a["info"].get("cpu",  {}).get("cores",    0) for a in healthy),
            "store_gb":      round(sum(a["info"].get("disk", {}).get("used_gb",  0) for a in healthy), 2),
        }
        return {
            "total":     len(agents),
            "healthy":   len(healthy),
            "resources": res,
            "agents": [
                {
                    "node_id":      a["node_id"],
                    "url":          a["url"],
                    "healthy":      a["healthy"],
                    "connected_at": a["connected_at"],
                    "last_ping":    a["last_ping"],
                    "ram":          a["info"].get("ram",   {}),
                    "disk":         a["info"].get("disk",  {}),
                    "cpu":          a["info"].get("cpu",   {}),
                    "proxy":        a["info"].get("proxy", {}),
                    "cache":        a["info"].get("cache", {}),
                }
                for a in agents
            ],
        }

    def cache_set(self, key: str, data: bytes) -> bool:
        ag = self._best_by_ram()
        if not ag: return False
        r = self._req(ag, "POST", f"/api/cache/set?key={key}", data=data)
        return bool(r)

    def cache_get(self, key: str) -> bytes | None:
        for ag in self._healthy():
            r = self._req(ag, "GET", f"/api/cache/get/{key}")
            if r: return r
        return None

    def cache_flush(self, prefix: str = "") -> int:
        total = 0
        for ag in self._healthy():
            r = self._json(ag, "POST", "/api/cache/flush", {"prefix": prefix})
            total += (r or {}).get("flushed", 0)
        return total

    def file_store(self, category: str, filename: str, data: bytes) -> bool:
        ag = self._best_by_disk()
        if not ag: return False
        r = self._req(ag, "PUT", f"/api/files/{category}/{filename}", data=data)
        return bool(r)

    def file_get(self, category: str, filename: str) -> bytes | None:
        for ag in self._healthy():
            r = self._req(ag, "GET", f"/api/files/{category}/{filename}")
            if r: return r
        return None

    def cpu_task(self, task_type: str, payload: dict,
                 wait: bool = True, timeout: int = 30) -> dict | None:
        ag = self._round_robin(task_type)
        if not ag: return None
        r  = self._json(ag, "POST", "/api/cpu/submit",
                        {"type": task_type, "payload": payload})
        if not r: return None
        tid = r.get("task_id")
        if not wait: return {"task_id": tid, "status": "pending"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            res = self._json(ag, "GET", f"/api/cpu/result/{tid}")
            if res and res.get("status") == "done":
                return res.get("result")
        return None

    def start_proxies(self) -> list:
        started = []
        for ag in self._healthy():
            r = self._json(ag, "POST", "/api/proxy/start",
                           {"host": "127.0.0.1", "port": 25565, "listen_port": 25565})
            if r and r.get("ok"):
                started.append(ag["node_id"])
        return started

    def stop_proxies(self):
        for ag in self._healthy():
            self._json(ag, "POST", "/api/proxy/stop")

    def archive_regions(self, older_than_days: int = 7) -> dict:
        best = self._best_by_disk()
        if not best:
            return {"ok": False, "error": "Sağlıklı agent yok"}
        archived, freed_mb = self._archive_regions(best, older_than_days)
        if archived > 0:
            self._expand_swap()
        return {"ok": True, "archived": archived, "freed_mb": round(freed_mb, 1)}

    # ──────────────────────────────────────────────────────────
    #  Yardımcılar
    # ──────────────────────────────────────────────────────────

    def set_socketio(self, sio):
        self._socketio = sio

    def _emit_update(self):
        if self._socketio:
            try: self._socketio.emit("pool_update", self.summary())
            except: pass

    def _log(self, msg: str):
        print(msg)
        # mc_panel'deki global log() fonksiyonu varsa oraya da gönder
        try:
            import urllib.request as _u2
            _u2.urlopen(_u2.Request(
                f"http://127.0.0.1:{PANEL_PORT}/api/internal/status_msg",
                data=json.dumps({"msg": msg}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ), timeout=2)
        except: pass


# ── Singleton ─────────────────────────────────────────────────
pool = ResourcePool()


# ── Flask Blueprint  (mc_panel.py'de app.register_blueprint ile bağlanır) ──

if _has_flask:
    pool_api = Blueprint("pool_api", __name__)

    @pool_api.route("/api/agent/register", methods=["POST"])
    def bp_register():
        d = request.json or {}
        if not d.get("tunnel") or not d.get("node_id"):
            return jsonify({"ok": False, "error": "tunnel / node_id eksik"}), 400
        pool.register(d["tunnel"], d["node_id"], d)
        return jsonify({"ok": True})

    @pool_api.route("/api/agent/heartbeat", methods=["POST"])
    def bp_heartbeat():
        d = request.json or {}
        if d.get("node_id") and d.get("tunnel"):
            pool.register(d["tunnel"], d["node_id"], d)
        return jsonify({"ok": True})

    @pool_api.route("/api/pool/status")
    def bp_status():
        return jsonify(pool.summary())

    @pool_api.route("/api/pool/cache/flush", methods=["POST"])
    def bp_cache_flush():
        prefix = (request.json or {}).get("prefix","")
        n = pool.cache_flush(prefix)
        return jsonify({"ok": True, "flushed": n})

    @pool_api.route("/api/pool/archive/regions", methods=["POST"])
    def bp_archive():
        days = int((request.json or {}).get("older_than_days", 7))
        return jsonify(pool.archive_regions(days))

    @pool_api.route("/api/pool/proxy/start", methods=["POST"])
    def bp_proxy_start():
        return jsonify({"ok": True, "started": pool.start_proxies()})

    @pool_api.route("/api/pool/proxy/stop", methods=["POST"])
    def bp_proxy_stop():
        pool.stop_proxies()
        return jsonify({"ok": True})

    @pool_api.route("/api/pool/task", methods=["POST"])
    def bp_task():
        d   = request.json or {}
        res = pool.cpu_task(d.get("type","echo"), d.get("payload",{}))
        return jsonify({"ok": True, "result": res} if res else {"ok": False})

    @pool_api.route("/api/pool/storage")
    def bp_storage():
        result = []
        for ag in pool._healthy():
            r = pool._json(ag, "GET", "/api/files/storage/stats")
            if r: r["node_id"] = ag["node_id"]; result.append(r)
        return jsonify({"agents": result})
