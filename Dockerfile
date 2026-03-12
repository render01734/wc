FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/root
ENV USER=root
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH=/usr/lib/jvm/java-21-openjdk-amd64/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ── ADIM 1: Temel araçlar ───────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget ca-certificates gnupg lsb-release locales \
    && locale-gen en_US.UTF-8 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── ADIM 2: Cloudflare Tunnel ──────────────────────────────
RUN curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
    https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    > /etc/apt/sources.list.d/cloudflared.list \
    && apt-get update && apt-get install -y cloudflared \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── ADIM 3: Java 21 + Python + araçlar ─────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jdk-headless \
    python3 python3-pip \
    net-tools procps psmisc iproute2 \
    htop vim nano git unzip zip \
    sudo util-linux \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /usr/share/doc/* \
              /usr/share/man/* /usr/share/info/*

# ── ADIM 4: socat + gcc (ağ araçları + userswap derleyici) ─
RUN apt-get update && apt-get install -y --no-install-recommends \
    socat gcc libc6-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── ADIM 5: Python paketleri ───────────────────────────────
RUN pip3 install --no-cache-dir \
    flask \
    flask-socketio \
    eventlet \
    requests \
    psutil \
    && pip3 cache purge \
    && rm -rf /root/.cache

# ── ADIM 6: Tam root + Minecraft dizinleri ──────────────────
RUN echo "root ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    && mkdir -p /minecraft/plugins /minecraft/backups /minecraft/config

# ── Uygulama ───────────────────────────────────────────────
# DÜZELTİLDİ: agent.py ve resource_pool.py de kopyalanıyor.
# main.py'deki run_agent_mode() /app/agent.py'yi çalıştırır,
# bu dosya imajda olmadan agent stub devreye giriyordu.
COPY main.py          /app/main.py
COPY mc_panel.py      /app/mc_panel.py
COPY agent.py         /app/agent.py
COPY resource_pool.py /app/resource_pool.py
COPY userswap.c       /app/userswap.c

# ── userswap.so'yu build aşamasında derle ──────────────────
# Runtime'da gcc gerekmez, LD_PRELOAD her zaman çalışır.
RUN mkdir -p /usr/local/lib \
    && gcc -O2 -shared -fPIC -o /usr/local/lib/userswap.so \
           /app/userswap.c -ldl -lpthread \
    && echo '[Dockerfile] userswap.so OK' \
    || echo '[Dockerfile] UYARI: userswap.so derlenemedi'

WORKDIR /app
EXPOSE 5000

CMD ["python3", "/app/main.py"]
