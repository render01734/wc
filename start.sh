#!/bin/bash
# Otomatik baslatici (LF formatinda kaydedilmeli!)

echo "[SISTEM] Hub Mimarisi (SQLite Zero-Config) Baslatiliyor..."

if [[ "$RENDER_EXTERNAL_HOSTNAME" == *"wc-yccy"* ]]; then
    export ENGINE_MODE="all"
    export DATA_DIR="/data"
    echo "[START] Otomatik ALL (Proxy+Game) modu algilandi: $RENDER_EXTERNAL_HOSTNAME"
else
    export ENGINE_MODE="gameserver"
    export SERVER_DIR="/server"
    export DATA_DIR="/server/world"
    export PROXY_URL="https://wc-yccy.onrender.com"
    echo "[START] GameServer modu algilandi — Proxy: $PROXY_URL"
fi

export PYTHONMALLOC=malloc

exec python3 /engine.py
