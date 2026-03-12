"""
⚙️  WORKER — 2. Render Hesabı
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bu servis 2. hesapta çalışır.
Diskini NBD (Network Block Device) olarak tünele açar.
Ana sunucu bu diski swap olarak kullanır.

Akış:
  1. 14GB blok dosya oluştur
  2. nbd-server ile port 10809'da sun
  3. cloudflared TCP tüneli → URL al
  4. URL'i MAIN_URL'e bildir (/api/worker/register)
  5. Sağlık endpoint'i tut (Render uyku yapmaya devam etsin)

⚠️  DEPRECATED (v10.0): Bu dosya artık kullanılmamaktadır.
   Kullanılacak dosya: agent.py
   NBD kaldırıldı → HTTP tabanlı Resource Pool kullanılıyor.
"""

import os, sys, subprocess, time, re, threading, json
import urllib.request
from flask import Flask, jsonify

PORT       = int(os.environ.get("PORT", "5000"))
MAIN_URL   = os.environ.get("MAIN_URL", "")   # Ana sunucu URL'i — env'den verilecek
NBD_PORT   = 10809
NBD_FILE   = "/nbd_disk.img"
NBD_GB     = 14    # Render 18GB disk limiti — 14GB güvenli

app        = Flask(__name__)
state      = {"nbd_ready": False, "tunnel": "", "nbd_gb": NBD_GB}


# ── Yardımcılar ────────────────────────────────────────────

def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, **kw)

def w(path, val):
    try:
        open(path, "w").write(str(val)); return True
    except: return False


# ══════════════════════════════════════════════════════════
#  ADIM 1 — 14GB blok dosya oluştur
# ══════════════════════════════════════════════════════════

def create_block_file():
    size_mb = NBD_GB * 1024

    if os.path.exists(NBD_FILE):
        gb = os.path.getsize(NBD_FILE) / 1024**3
        if gb >= NBD_GB * 0.9:
            print(f"[Worker] ✅ Blok dosya zaten var: {gb:.1f}GB")
            state["nbd_ready"] = True
            return

    print(f"[Worker] 💾 {NBD_GB}GB blok dosya oluşturuluyor (fallocate)...")
    ret = sh(f"fallocate -l {size_mb}M {NBD_FILE}")
    if ret.returncode != 0:
        print("[Worker] ⚠️  fallocate yok → dd ile oluşturuluyor (yavaş)...")
        # 1GB parçalar halinde oluştur
        for i in range(NBD_GB):
            sh(f"dd if=/dev/zero of={NBD_FILE} bs=64M count=16 "
               f"seek={i*16} oflag=seek_bytes 2>/dev/null || "
               f"dd if=/dev/zero of={NBD_FILE} bs=64M count=16 >> /dev/null")
            print(f"[Worker]   {i+1}/{NBD_GB}GB")

    gb = os.path.getsize(NBD_FILE) / 1024**3
    print(f"[Worker] ✅ Blok dosya hazır: {gb:.1f}GB")
    state["nbd_ready"] = True


# ══════════════════════════════════════════════════════════
#  ADIM 2 — nbd-server başlat
# ══════════════════════════════════════════════════════════

def start_nbd_server():
    sh("modprobe nbd max_part=0 2>/dev/null")
    os.makedirs("/etc/nbd-server", exist_ok=True)

    # nbd-server config
    open("/etc/nbd-server/config", "w").write(f"""
[generic]
    port = {NBD_PORT}
    allowlist = true

[disk]
    exportname = {NBD_FILE}
    readonly = false
    flush = true
    fua = true
    rotational = false
""")

    print(f"[Worker] 🔌 nbd-server başlatılıyor (:{NBD_PORT})...")
    proc = subprocess.Popen(
        ["nbd-server", "-C", "/etc/nbd-server/config"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    time.sleep(2)

    if proc.poll() is None:
        print(f"[Worker] ✅ nbd-server aktif (port {NBD_PORT})")
        return True

    # nbd-server yoksa socat ile raw TCP fallback
    print("[Worker] ⚠️  nbd-server yok → socat TCP fallback...")
    subprocess.Popen([
        "socat", f"TCP-LISTEN:{NBD_PORT},reuseaddr,fork",
        f"FILE:{NBD_FILE},rdwr,creat"
    ])
    time.sleep(1)
    print(f"[Worker] ✅ socat TCP bridge aktif (port {NBD_PORT})")
    return True


# ══════════════════════════════════════════════════════════
#  ADIM 3 — Cloudflare TCP tüneli
# ══════════════════════════════════════════════════════════

def start_tunnel():
    log = "/tmp/cf_worker.log"
    print(f"[Worker] 🌐 Cloudflare TCP tüneli açılıyor...")

    subprocess.Popen([
        "cloudflared", "tunnel",
        "--url", f"tcp://localhost:{NBD_PORT}",
        "--no-autoupdate", "--loglevel", "info",
    ], stdout=open(log, "w"), stderr=subprocess.STDOUT)

    for _ in range(120):
        try:
            content = open(log).read()
            urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', content)
            if urls:
                url  = urls[0]
                host = url.replace("https://", "")
                state["tunnel"] = url

                print(f"\n[Worker] ╔══════════════════════════════════════════╗")
                print(f"[Worker] ║  ✅ NBD TÜNEL HAZIR                       ║")
                print(f"[Worker] ║                                           ║")
                print(f"[Worker] ║  Host : {host:<33}║")
                print(f"[Worker] ║  Port : {NBD_PORT:<33}║")
                print(f"[Worker] ╚══════════════════════════════════════════╝")
                print(f"\n[Worker] → Ana sunucu render.yaml'ına ekle:")
                print(f"[Worker]   WORKER_HOST = {host}")
                print()

                _notify_main(url, host)
                return url
        except: pass
        time.sleep(0.5)

    print("[Worker] ⚠️  Tünel URL alınamadı")
    return ""


def _notify_main(url, host):
    if not MAIN_URL:
        return
    try:
        data = json.dumps({"worker_url": url, "worker_host": host,
                           "nbd_gb": NBD_GB}).encode()
        req = urllib.request.Request(
            f"{MAIN_URL.rstrip('/')}/api/worker/register",
            data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[Worker] ✅ Ana sunucuya bildirildi: {MAIN_URL}")
    except Exception as e:
        print(f"[Worker] ⚠️  Bildirim hatası: {e}")


# ══════════════════════════════════════════════════════════
#  FLASK — sağlık kontrolü
# ══════════════════════════════════════════════════════════

@app.route("/")
@app.route("/health")
def health():
    return jsonify({
        "status":    "ok",
        "nbd_ready": state["nbd_ready"],
        "nbd_gb":    NBD_GB,
        "tunnel":    state["tunnel"],
    })

@app.route("/api/worker/status")
def worker_status():
    return jsonify(state)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

print("\n" + "━"*50)
print("  ⚙️   Worker Servisi — Disk Paylaşımı v1.0")
print(f"      NBD={NBD_GB}GB | ANA={MAIN_URL or '(MAIN_URL ayarlanmadı)'}")
print("━"*50 + "\n")

# Sırayla: dosya → nbd → tünel
create_block_file()
start_nbd_server()
threading.Thread(target=start_tunnel, daemon=True).start()

print(f"[Worker] Flask sağlık sunucusu :{PORT}...")
app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
