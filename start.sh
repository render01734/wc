#!/bin/bash
# Otomatik başlatıcı — Render hostname'e göre otomatik mod seçer

if [[ "$RENDER_EXTERNAL_HOSTNAME" == *"wc-yccy"* ]]; then
    export ENGINE_MODE="proxy"
    export DATA_DIR="/data"
    echo "[START] Otomatik Proxy modu algilandi: $RENDER_EXTERNAL_HOSTNAME"
else
    export ENGINE_MODE="gameserver"
    export SERVER_DIR="/server"
    export DATA_DIR="/server/world"
    
    export PROXY_URL="https://wc-yccy.onrender.com"
    
    # DİKKAT: Uyandırma sisteminin çalışması için RENDER_EXTERNAL_HOSTNAME alındı
    export SERVER_LABEL="${RENDER_EXTERNAL_HOSTNAME:-$(hostname)}"
    
    echo "[START] GameServer modu algilandi — Proxy: $PROXY_URL — Etiket: $SERVER_LABEL"
fi

exec python3 /engine.py
