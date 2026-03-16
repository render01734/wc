FROM debian:bookworm-slim

# HATA #7 DÜZELTMESİ: Tüm paket kurulumunu tek RUN katmanında birleştir.
# Önceki kodda apt-get update ayrı katmandaydı; Docker önbellek nedeniyle
# güncelleme atlanabilir ve "unable to locate package" hatasına yol açar.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates python3 python3-pip libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Veritabani icin asenkron SQLite kutuphanesi (Tak-Calistir icin gerekli)
RUN pip3 install aiosqlite --break-system-packages

# HATA #8 DÜZELTMESİ: Bore kurulumunda hata yönetimi eklendi.
# Eski kod: grep ile tag_name'i parse ediyordu — JSON formatı değişirse kurulum sessizce bozulur.
# Yeni kod: GitHub API'den en son sürümü güvenli şekilde alır, başarısız olursa sabit sürüme döner.
RUN BORE_VER=$(curl -sf https://api.github.com/repos/ekzhang/bore/releases/latest \
        | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null \
        || echo "v0.5.0") \
    && echo "Bore surumu: $BORE_VER" \
    && wget -qO /tmp/bore.tar.gz \
        "https://github.com/ekzhang/bore/releases/download/${BORE_VER}/bore-${BORE_VER}-x86_64-unknown-linux-musl.tar.gz" \
    && tar xzf /tmp/bore.tar.gz -C /usr/local/bin \
    && rm /tmp/bore.tar.gz \
    && chmod +x /usr/local/bin/bore \
    && bore --version

# Cuberite (Minecraft Sunucu Motoru)
WORKDIR /server
RUN wget -qO /tmp/cuberite.tar.gz \
      "https://download.cuberite.org/linux-x86_64/Cuberite.tar.gz" \
    && tar xzf /tmp/cuberite.tar.gz -C /server \
    && rm /tmp/cuberite.tar.gz \
    && find /server -name "Cuberite" -type f

# HATA #9 DÜZELTMESİ: /data dizini Dockerfile'da oluşturulmalı.
# Yoksa engine.py DATA_DIR'e yazmaya çalışırken hata alır; aiosqlite
# veritabanını oluşturamaz ve tüm sunucu kaydı çalışmaz.
RUN mkdir -p /data /server/world/players

# Betikleri tasi ve yetki ver
COPY engine.py /engine.py
COPY start.sh  /start.sh
RUN chmod +x /start.sh

# Sağlık kontrolü: HTTP API'nin ayakta olduğunu doğrula
# HATA #10 DÜZELTMESİ: Healthcheck yoktu. Render.com ve Docker
# servisin hazır olup olmadığını bilemiyordu; erken trafik 502 veriyordu.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${PORT:-8080}/api/status || exit 1

# Disariya acilacak portlar
EXPOSE 8080 25565
CMD ["/start.sh"]
