FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/root
ENV USER=root
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH=/usr/lib/jvm/java-21-openjdk-amd64/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ── ADIM 1: Temel araçlar + locale ─────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg lsb-release locales \
    && locale-gen en_US.UTF-8 \
    # Gereksiz locale'leri sil (sadece en_US kalsın) — ~60MB tasarruf
    && find /usr/share/locale -mindepth 1 -maxdepth 1 \
       ! -name 'en' ! -name 'en_US' ! -name 'en_US.UTF-8' -exec rm -rf {} + 2>/dev/null || true \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── ADIM 2: Cloudflare Tunnel ───────────────────────────────────────────────
RUN curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
    https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    > /etc/apt/sources.list.d/cloudflared.list \
    && apt-get update && apt-get install -y --no-install-recommends cloudflared \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── ADIM 3: Java 21 JRE + Python + minimal araçlar ─────────────────────────
# JRE (runtime) kullanıyoruz, JDK değil → ~35MB daha küçük image + daha az RAM
# jemalloc: bellek parçalanmasını azaltır → uzun süreli çalışmada RSS büyümesini önler
# unzip/zip: plugin kurulumu için gerekli
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless \
    python3 python3-pip \
    libjemalloc2 \
    procps net-tools \
    gcc libc6-dev \
    unzip zip \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
              /usr/share/doc/* /usr/share/man/* /usr/share/info/* \
    # JVM debug/demo dosyalarını sil — runtime'da gerekmez
    && find /usr/lib/jvm -name "*.diz" -delete 2>/dev/null || true \
    && find /usr/lib/jvm -name "*.debuginfo" -delete 2>/dev/null || true \
    && rm -rf /usr/lib/jvm/java-21-openjdk-amd64/demo 2>/dev/null || true \
    && rm -rf /usr/lib/jvm/java-21-openjdk-amd64/sample 2>/dev/null || true

# ── ADIM 4: Python paketleri ────────────────────────────────────────────────
RUN pip3 install --no-cache-dir \
    flask \
    flask-socketio \
    eventlet \
    psutil \
    && pip3 cache purge \
    && rm -rf /root/.cache /tmp/*

# ── ADIM 5: Uygulama + dizinler ─────────────────────────────────────────────
RUN echo "root ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    && mkdir -p /minecraft/plugins /minecraft/backups /minecraft/config \
                /usr/local/lib

COPY main.py          /app/main.py
COPY mc_panel.py      /app/mc_panel.py
COPY agent.py         /app/agent.py
COPY resource_pool.py /app/resource_pool.py
COPY userswap.c       /app/userswap.c

# ── ADIM 6: userswap.so derle + gcc kaldır ──────────────────────────────────
# Build-time'da derle → runtime'da gcc yok (imaj küçülür, saldırı yüzeyi azalır)
RUN gcc -O3 -shared -fPIC \
        -o /usr/local/lib/userswap.so /app/userswap.c \
        -ldl -lpthread \
    && echo '[Dockerfile] ✅ userswap.so (O3) derlendi' \
    && apt-get remove -y gcc libc6-dev \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /app/userswap.c

# ── ADIM 7: Jemalloc path tespiti ───────────────────────────────────────────
# Python & Java'nın dinamik linker'ı bu yolu bulabilmesi için symlink oluştur
RUN JEMALLOC=$(find /usr/lib -name "libjemalloc.so*" 2>/dev/null | head -1) \
    && if [ -n "$JEMALLOC" ]; then \
         ln -sf "$JEMALLOC" /usr/local/lib/libjemalloc.so; \
         echo "[Dockerfile] ✅ jemalloc: $JEMALLOC"; \
       fi

# ── OS optimizasyonları: sysctl için root gerekiyor (runtime'da yapılıyor) ──
# Render'da privileged mod yok ama /proc/sys yazma izni var (bazıları)

WORKDIR /app
EXPOSE 5000

CMD ["python3", "/app/main.py"]
