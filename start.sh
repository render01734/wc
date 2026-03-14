#!/bin/bash
# Game Server başlatıcı — engine.py tüm işi yapar
export ENGINE_MODE=gameserver
export SERVER_DIR=/server
export DATA_DIR=/server/world
# İsteğe bağlı: proxy URL ve sunucu etiketi
# export PROXY_URL=https://wc-tsgd.onrender.com
# export SERVER_LABEL=GameServer-1
exec python3 /engine.py
