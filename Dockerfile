FROM debian:bookworm-slim

# ── Sistem bağımlılıkları (tek katman) ───────────────────────────────────────
# NOT: apt-get update ve install aynı RUN katmanında — önbellekleme kaynaklı
# "unable to locate package" hatasını önler.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        curl \
        ca-certificates \
        python3 \
        python3-pip \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# ── Python bağımlılıkları ─────────────────────────────────────────────────────
RUN pip3 install aiosqlite --break-system-packages --quiet

# ── Bore tünel aracı ─────────────────────────────────────────────────────────
# GitHub API'den en son sürümü çek; başarısız olursa sabit sürüme dön.
RUN set -e; \
    BORE_VER=$(curl -sf https://api.github.com/repos/ekzhang/bore/releases/latest \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null \
        || echo "v0.5.0") \
    && echo "[BUILD] Bore sürümü: $BORE_VER" \
    && wget -qO /tmp/bore.tar.gz \
        "https://github.com/ekzhang/bore/releases/download/${BORE_VER}/bore-${BORE_VER}-x86_64-unknown-linux-musl.tar.gz" \
    && tar xzf /tmp/bore.tar.gz -C /usr/local/bin \
    && rm /tmp/bore.tar.gz \
    && chmod +x /usr/local/bin/bore \
    && bore --version

# ── Cuberite (Minecraft Sunucu Motoru) ───────────────────────────────────────
WORKDIR /server
RUN wget -qO /tmp/cuberite.tar.gz \
        "https://download.cuberite.org/linux-x86_64/Cuberite.tar.gz" \
    && tar xzf /tmp/cuberite.tar.gz -C /server \
    && rm /tmp/cuberite.tar.gz

# ── Dizin yapısı ─────────────────────────────────────────────────────────────
# Render.com'da kalıcı disk geç bağlanabilir; temel dizinleri önceden oluştur.
RUN mkdir -p /data /data/players /server/world/players

# ── Uygulama dosyaları ────────────────────────────────────────────────────────
COPY engine.py /engine.py
COPY start.sh  /start.sh
RUN chmod +x /start.sh

# ── Güvenlik: root dışı kullanıcı ────────────────────────────────────────────
# BUG FIX: Önceden tüm süreçler root olarak çalışıyordu (güvenlik açığı).
# Yeni: appuser oluşturuluyor, yalnızca gerekli dizinlere yazma yetkisi veriliyor.
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && chown -R appuser:appuser /data /server /engine.py /start.sh
USER appuser

# ── Sağlık Kontrolü ──────────────────────────────────────────────────────────
# /api/status endpoint'i hem HTTP sunucusunun ayakta olduğunu hem de
# temel veritabanı bağlantısını doğrular.
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=20s \
    --retries=3 \
    CMD curl -sf "http://localhost:${PORT:-8080}/api/status" || exit 1

# ── Port bildirimleri ─────────────────────────────────────────────────────────
EXPOSE 8080 25565

CMD ["/start.sh"]
