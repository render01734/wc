FROM ubuntu:24.04
# ── Cuberite C++ Minecraft Server (1.8.8 uyumlu) ──────────────
# Cuberite C++ binary — JVM yok → ~50MB RAM (Java 400MB yerine)
# Cuberite binary build sırasında indirilir → soğuk başlatma anında

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=en_US.UTF-8

# ── Sistem paketleri ─────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-dev \
    gcc make \
    wget curl ca-certificates \
    procps util-linux kmod iproute2 \
    libstdc++6 libgcc-s1 \
    libssl3 libssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── cloudflared (Cloudflare Tunnel) ─────────────────────────────────────────
RUN wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

# ── Python bağımlılıkları ────────────────────────────────────────────────────
RUN pip3 install --no-cache-dir --break-system-packages \
    flask flask-socketio eventlet psutil \
    && pip3 cache purge && rm -rf /root/.cache /tmp/*

# ── Uygulama kaynak dosyaları ────────────────────────────────────────────────
WORKDIR /app
COPY mc_panel.py  ./mc_panel.py
COPY cluster.py   ./cluster.py
COPY main.py      ./main.py
COPY agent.py     ./agent.py
# resource_pool.py kaldırıldı — v14'te cluster.py kullanılıyor
# userswap.c dahil edilmiyor — Cuberite C++ mmap hook gerektirmiyor

# ── Dizin yapısı ─────────────────────────────────────────────────────────────
RUN mkdir -p \
    /minecraft/world/region \
    /minecraft/world_nether/DIM-1/region \
    /minecraft/world_the_end/DIM1/region \
    /minecraft/config \
    /mnt/vcluster \
    /tmp/cluster_cache \
    /agent_data/regions/world \
    /agent_data/regions/world_nether \
    /agent_data/regions/world_the_end \
    /agent_data/backups \
    /agent_data/plugins \
    /agent_data/configs \
    /agent_data/chunks \
    /agent_data/cuberite_cache

# ── Cuberite binary — build-time indir ──────────────────────────────────────
# Build sırasında indirilirse container soğuk başlatması ~60sn kısalır.
# İndirme başarısız olursa Python runtime'da download_cuberite() devreye girer.
RUN wget -q --timeout=120 \
    "https://download.cuberite.org/linux-x86_64/Cuberite.tar.gz" \
    -O /tmp/cuberite.tar.gz \
    && tar -xzf /tmp/cuberite.tar.gz -C /minecraft \
    && find /minecraft -name "Cuberite" -type f -exec chmod +x {} \; \
    && rm -f /tmp/cuberite.tar.gz \
    && echo "[Dockerfile] ✅ Cuberite build-time indirildi" \
    || echo "[Dockerfile] ⚠️  Cuberite build-time indirilemedi — runtime'da indirilecek"

# ── gcc temizliği (Cuberite için gerekmez, runtime'da yok) ───────────────────
RUN apt-get remove -y gcc make python3-dev \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 5000 8080 25565

# main.py IS_MAIN kontrolü:
#   RENDER_EXTERNAL_URL == wc-tsgd.onrender.com → ANA mod (panel + MC)
#   diğer URL → AGENT mod (agent.py başlatır)
CMD ["python3", "/app/main.py"]
