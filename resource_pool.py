"""
🔗  ResourcePool — Agent Kaynak Havuzu Yöneticisi  v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
main.py tarafından import edilir.
Agent API'lerini soyutlar:
  - RAM Cache   (chunk/entity verisi offload)
  - File Store  (region dosyası arşivi, serbest disk → büyük swap)
  - CPU Worker  (sıkıştırma, hash, istatistik görevleri)
  - TCP Proxy   (oyuncu bağlantı iletimi)
  - Sağlık izleme + otomatik yük dengeleme
"""

import threading, time, json, random, os, hashlib, shutil
import urllib.request as _ur
import urllib.error
from pathlib import Path
from typing import Optional

MAIN_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://wc-tsgd.onrender.com")
MC_DIR   = Path("/minecraft")

# ─────────────────────────────────────────────────────────────
class AgentClient:
    """Tek bir agent'a HTTP istekleri gönderir."""

    def __init__(self, tunnel_url: str, node_id: str, info: dict):
        self.url     = tunnel_url.rstrip("/")
        self.node_id = node_id
        self.info    = info
        self.healthy = True
        self.last_ok = time.time()
        self._fail   = 0

    # ── HTTP yardımcıları ────────────────────────────────────

    def _req(self, method: str, path: str,
             data: bytes = None, headers: dict = None,
             timeout: int = 15) -> Optional[bytes]:
        try:
            req = _ur.Request(
                self.url + path,
                data=data,
                headers={"Content-Type": "application/octet-stream",
                         **(headers or {})},
                method=method,
            )
            with _ur.urlopen(req, timeout=timeout) as r:
                result = r.read()
                self.healthy = True
                self.last_ok = time.time()
                self._fail   = 0
                return result
        except Exception as e:
            self._fail += 1
            if self._fail >= 3:
                self.healthy = False
            return None

    def _json(self, method: str, path: str, body: dict = None) -> Optional[dict]:
        raw = self._req(
            method, path,
            data=json.dumps(body).encode() if body else None,
            headers={"Content-Type": "application/json"},
        )
        if raw:
            try:
                return json.loads(raw)
            except:
                pass
        return None

    # ── RAM Cache ────────────────────────────────────────────

    def cache_set(self, key: str, data: bytes) -> bool:
        r = self._req("POST", f"/api/cache/set?key={key}", data=data)
        return r is not None

    def cache_get(self, key: str) -> Optional[bytes]:
        return self._req("GET", f"/api/cache/get/{key}")

    def cache_delete(self, key: str) -> bool:
        r = self._json("POST", f"/api/cache/delete/{key}")
        return bool(r and r.get("ok"))

    def cache_stats(self) -> dict:
        return self._json("GET", "/api/cache/stats") or {}

    def cache_flush(self, prefix: str = "") -> dict:
        return self._json("POST", "/api/cache/flush", {"prefix": prefix}) or {}

    # ── File Store ───────────────────────────────────────────

    def file_upload(self, category: str, filename: str, data: bytes) -> bool:
        r = self._req("PUT", f"/api/files/{category}/{filename}", data=data, timeout=120)
        return r is not None

    def file_download(self, category: str, filename: str) -> Optional[bytes]:
        return self._req("GET", f"/api/files/{category}/{filename}", timeout=120)

    def file_exists(self, category: str, filename: str) -> bool:
        r = self._json("GET", f"/api/files/{category}/{filename}/exists")
        return bool(r and r.get("exists"))

    def file_delete(self, category: str, filename: str) -> bool:
        r = self._req("DELETE", f"/api/files/{category}/{filename}")
        return r is not None

    def file_list(self, category: str) -> list:
        r = self._json("GET", f"/api/files/{category}")
        return (r or {}).get("files", [])

    def storage_stats(self) -> dict:
        return self._json("GET", "/api/files/storage/stats") or {}

    # ── CPU Worker ───────────────────────────────────────────

    def submit_task(self, task_type: str, payload: dict) -> Optional[str]:
        r = self._json("POST", "/api/cpu/submit", {"type": task_type, "payload": payload})
        return (r or {}).get("task_id")

    def get_task_result(self, task_id: str, wait: bool = False, timeout: int = 30) -> Optional[dict]:
        deadline = time.time() + timeout
        while True:
            r = self._json("GET", f"/api/cpu/result/{task_id}")
            if r and r.get("status") == "done":
                return r.get("result")
            if not wait or time.time() > deadline:
                return None
            time.sleep(1)

    # ── Proxy ────────────────────────────────────────────────

    def proxy_start(self, target_host: str, target_port: int = 25565,
                    listen_port: int = 25565) -> bool:
        r = self._json("POST", "/api/proxy/start", {
            "host": target_host, "port": target_port, "listen_port": listen_port
        })
        return bool(r and r.get("ok"))

    def proxy_stop(self) -> bool:
        r = self._json("POST", "/api/proxy/stop")
        return bool(r and r.get("ok"))

    def proxy_status(self) -> dict:
        return self._json("GET", "/api/proxy/status") or {}

    # ── Genel ────────────────────────────────────────────────

    def get_status(self) -> dict:
        return self._json("GET", "/api/status") or {}


# ─────────────────────────────────────────────────────────────
class ResourcePool:
    """
    Tüm agent'ları yönetir.
    Yük dengeleme: en az yüklü agent'a yönlendir.
    """

    def __init__(self):
        self.agents: dict[str, AgentClient] = {}
        self.lock   = threading.Lock()
        self._log_cb = print   # mc_panel.py log() fonksiyonu inject edilir

    def set_logger(self, fn):
        self._log_cb = fn

    def _log(self, msg: str):
        try:
            self._log_cb(msg)
        except:
            print(msg)

    # ── Agent kaydı / keşif ──────────────────────────────────

    def register(self, tunnel_url: str, node_id: str, info: dict):
        with self.lock:
            if node_id in self.agents:
                # Güncelle
                self.agents[node_id].info    = info
                self.agents[node_id].url     = tunnel_url.rstrip("/")
                self.agents[node_id].healthy = True
                return
        agent = AgentClient(tunnel_url, node_id, info)
        with self.lock:
            self.agents[node_id] = agent
        self._log(
            f"[Pool] ✅ Agent kaydı: {node_id} | "
            f"RAM:{info.get('ram',{}).get('free_mb',0)}MB boş | "
            f"Disk:{info.get('disk',{}).get('free_gb',0)}GB boş"
        )
        # Büyük disk varsa ana sunucuya swap için alan aç
        threading.Thread(target=self._offload_old_regions_to,
                         args=(agent,), daemon=True).start()

    def remove(self, node_id: str):
        with self.lock:
            self.agents.pop(node_id, None)

    def get_agents(self, healthy_only=True) -> list[AgentClient]:
        with self.lock:
            agents = list(self.agents.values())
        if healthy_only:
            agents = [a for a in agents if a.healthy]
        return agents

    def agent_count(self) -> int:
        return len(self.get_agents())

    def total_resources(self) -> dict:
        agents = self.get_agents()
        return {
            "agents":        len(agents),
            "ram_cache_mb":  sum(a.info.get("ram", {}).get("cache_mb",  0) for a in agents),
            "ram_free_mb":   sum(a.info.get("ram", {}).get("free_mb",   0) for a in agents),
            "disk_store_gb": sum(a.info.get("disk",{}).get("store_gb",  0) for a in agents),
            "disk_free_gb":  sum(a.info.get("disk",{}).get("free_gb",   0) for a in agents),
            "cpu_cores":     sum(a.info.get("cpu", {}).get("cores",     0) for a in agents),
        }

    # ── Yük dengeleme ────────────────────────────────────────

    def _least_loaded(self) -> Optional[AgentClient]:
        """En az yüklü sağlıklı agent'ı döndür."""
        agents = self.get_agents()
        if not agents:
            return None
        return min(agents, key=lambda a: a.info.get("ram", {}).get("cache_mb", 999))

    def _most_disk(self) -> Optional[AgentClient]:
        """En çok boş diski olan agent'ı döndür."""
        agents = self.get_agents()
        if not agents:
            return None
        return max(agents, key=lambda a: a.info.get("disk", {}).get("free_gb", 0))

    # ── RAM Cache API ────────────────────────────────────────

    def cache_set(self, key: str, data: bytes, prefer_agent: str = None) -> bool:
        agent = (self.agents.get(prefer_agent) if prefer_agent
                 else self._least_loaded())
        if not agent:
            return False
        return agent.cache_set(key, data)

    def cache_get(self, key: str) -> Optional[bytes]:
        """Tüm agent'larda ara (önce en az yüklü)."""
        agents = self.get_agents()
        # Belirleyici arama: key hash'ine göre agent seç, hız için
        if agents:
            idx   = int(hashlib.md5(key.encode()).hexdigest(), 16) % len(agents)
            first = agents[idx]
            data  = first.cache_get(key)
            if data is not None:
                return data
            # Miss → diğerlerini de dene
            for a in agents:
                if a.node_id != first.node_id:
                    data = a.cache_get(key)
                    if data is not None:
                        return data
        return None

    def cache_flush_all(self, prefix: str = "") -> int:
        total = 0
        for agent in self.get_agents():
            r = agent.cache_flush(prefix)
            total += (r or {}).get("flushed", 0)
        return total

    # ── File Store API ───────────────────────────────────────

    def store_region(self, dimension: str, region_file: Path) -> bool:
        """Region dosyasını en çok boş diski olan agent'a yükle."""
        agent = self._most_disk()
        if not agent:
            return False
        data = region_file.read_bytes()
        ok   = agent.file_upload("regions", f"{dimension}/{region_file.name}", data)
        if ok:
            self._log(f"[Pool] 📤 Region arşivlendi: {dimension}/{region_file.name} → {agent.node_id}")
        return ok

    def fetch_region(self, dimension: str, filename: str,
                     dest: Path) -> bool:
        """Agent'lardan region dosyasını indir."""
        for agent in self.get_agents():
            data = agent.file_download("regions", f"{dimension}/{filename}")
            if data:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                self._log(f"[Pool] 📥 Region indirildi: {filename} ← {agent.node_id}")
                return True
        return False

    def region_exists_remote(self, dimension: str, filename: str) -> bool:
        for agent in self.get_agents():
            if agent.file_exists("regions", f"{dimension}/{filename}"):
                return True
        return False

    def list_remote_regions(self, dimension: str) -> list[str]:
        seen = set()
        for agent in self.get_agents():
            for f in agent.file_list("regions"):
                name = f["name"]
                if name.endswith(".mca"):
                    seen.add(name)
        return sorted(seen)

    # ── CPU Worker API ───────────────────────────────────────

    def run_task(self, task_type: str, payload: dict,
                 wait: bool = True, timeout: int = 60) -> Optional[dict]:
        """En az yüklü agent'ta görev çalıştır."""
        agent = self._least_loaded()
        if not agent:
            return None
        task_id = agent.submit_task(task_type, payload)
        if not task_id:
            return None
        if wait:
            return agent.get_task_result(task_id, wait=True, timeout=timeout)
        return {"task_id": task_id, "agent": agent.node_id}

    def compress_backup(self, source_path: str) -> Optional[dict]:
        return self.run_task("compress_file", {"path": source_path})

    # ── Proxy API ────────────────────────────────────────────

    def start_proxies(self, mc_host: str, mc_port: int = 25565) -> list[str]:
        """Tüm agent'larda TCP relay proxy başlat."""
        started = []
        for agent in self.get_agents():
            ok = agent.proxy_start(mc_host, mc_port, listen_port=25565)
            if ok:
                started.append(agent.url)
                self._log(f"[Pool] 🔀 Proxy başlatıldı: {agent.node_id} → {mc_host}:{mc_port}")
        return started

    def stop_proxies(self):
        for agent in self.get_agents():
            agent.proxy_stop()

    # ── Disk-to-Swap optimizasyonu ───────────────────────────

    def _offload_old_regions_to(self, agent: AgentClient):
        """
        Ana sunucuda eski/nadir erişilen region dosyalarını agent'a taşır.
        Böylece ana sunucuda disk alanı açılır → daha büyük swap dosyası.
        """
        import time as _t
        _t.sleep(30)   # Sunucu stabil olana kadar bekle

        threshold_days = 7    # 7 günden eski region'ları taşı
        freed_mb       = 0
        now            = _t.time()

        for dim_dir in ["world/region", "world_nether/DIM-1/region", "world_the_end/DIM1/region"]:
            region_dir = MC_DIR / dim_dir
            if not region_dir.exists():
                continue
            dim_name = dim_dir.split("/")[0]

            for region_file in sorted(region_dir.glob("*.mca"),
                                      key=lambda f: f.stat().st_mtime):
                age_days = (now - region_file.stat().st_mtime) / 86400
                size_mb  = region_file.stat().st_size / 1e6

                if age_days < threshold_days:
                    continue  # Yeni region, dokunma
                if self.region_exists_remote(dim_name, region_file.name):
                    continue  # Zaten uzakta

                # Disk doluluk kontrolü
                import shutil as _sh
                disk_free_gb = _sh.disk_usage("/").free / 1e9
                if disk_free_gb > 8.0:
                    break   # Yeterli yer var, gerek yok

                ok = self.store_region(dim_name, region_file)
                if ok:
                    # Yerel kopyayı sil (Minecraft kapalıysa)
                    try:
                        region_file.unlink()
                        freed_mb += size_mb
                    except:
                        pass

        if freed_mb > 10:
            self._log(f"[Pool] 💾 {freed_mb:.0f}MB region arşivlendi → swap için disk açıldı")
            # Yeni swap boyutunu güncelle
            self._expand_swap()

    def _expand_swap(self):
        """Açılan disk alanını değerlendirip swap dosyasını büyüt."""
        import shutil, subprocess as sp
        disk_free_gb = shutil.disk_usage("/").free / 1e9
        target_swap_gb = min(6.0, disk_free_gb * 0.6)
        target_mb = int(target_swap_gb * 1024)

        sf = "/swapfile2"
        sp.run(f"swapoff {sf} 2>/dev/null", shell=True)
        try:
            if os.path.exists(sf): os.remove(sf)
        except: pass

        r = sp.run(f"fallocate -l {target_mb}M {sf}", shell=True, capture_output=True)
        if r.returncode != 0:
            sp.run(f"dd if=/dev/zero of={sf} bs=64M count={max(1,target_mb//64)} status=none",
                   shell=True)
        sp.run(f"chmod 600 {sf} && mkswap -f {sf} && swapon -p 1 {sf}",
               shell=True, capture_output=True)
        self._log(f"[Pool] ✅ Swap genişletildi: {target_mb}MB ({sf})")

    # ── Sağlık izleme ────────────────────────────────────────

    def health_monitor(self, interval: int = 30):
        """Arka planda agent sağlığını izle."""
        while True:
            time.sleep(interval)
            for agent in list(self.agents.values()):
                status = agent.get_status()
                if status:
                    agent.info    = status
                    agent.healthy = True
                    agent.last_ok = time.time()
                    agent._fail   = 0
                else:
                    agent._fail += 1
                    if agent._fail >= 3:
                        agent.healthy = False

    def summary(self) -> dict:
        agents = self.get_agents(healthy_only=False)
        return {
            "total":    len(agents),
            "healthy":  sum(1 for a in agents if a.healthy),
            "resources": self.total_resources(),
            "agents":   [
                {
                    "node_id": a.node_id,
                    "url":     a.url,
                    "healthy": a.healthy,
                    "ram":     a.info.get("ram", {}),
                    "disk":    a.info.get("disk", {}),
                    "cpu":     a.info.get("cpu", {}),
                    "proxy":   a.info.get("proxy", {}),
                    "last_ok": int(a.last_ok),
                }
                for a in agents
            ],
        }


# ── Singleton ────────────────────────────────────────────────
pool = ResourcePool()

# Arka plan izleyici başlat
threading.Thread(target=pool.health_monitor, daemon=True).start()
