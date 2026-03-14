#!/bin/bash
# Otomatik başlatıcı — yeni sunucu açınca hiçbir şey değiştirme

MODE="${ENGINE_MODE:-gameserver}"

if [ "$MODE" = "proxy" ]; then
    export DATA_DIR="${DATA_DIR:-/data}"
    echo "[START] Proxy modu — wc-tsgd ana sunucu"
else
    export ENGINE_MODE=gameserver
    export SERVER_DIR="${SERVER_DIR:-/server}"
    export DATA_DIR="${DATA_DIR:-/server/world}"

    # Ana proxy — sabit, değişmez
    export PROXY_URL="https://wc-yccy.onrender.com"

    # Etiket: hostname'den otomatik üret (her Render servisi unique hostname alır)
    export SERVER_LABEL="${SERVER_LABEL:-$(hostname)}"

    echo "[START] GameServer modu — Proxy: $PROXY_URL — Etiket: $SERVER_LABEL"
fi

exec python3 /engine.py
