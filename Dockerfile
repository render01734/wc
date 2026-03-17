FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY engine.py /app/engine.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Standart kullanıcıya geri dönüldü (Swap için root yetkisine gerek kalmadı)
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
CMD ["/app/start.sh"]
