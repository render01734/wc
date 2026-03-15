#!/bin/bash
# Otomatik baslatici (Kesinlikle LF formatinda kaydedilmeli!)

echo "[SISTEM] Baslatma dizisi basliyor..."

# Render'in verdigi karmasik isimleri (wc-1-9h3p vb.) zorla kullanmasini engelledik.
# Artik isimlendirme (GM1, GM2) tamamen Python tarafindan otomatik yapilacak.

if [[ "$RENDER_EXTERNAL_HOSTNAME" == *"wc-yccy"* ]]; then
    # Eger baglanti url'sinde wc-yccy varsa bu sunucu MERKEZ HUB'dir
    export ENGINE_MODE="all"
    export DATA_DIR="/data"
    echo "[START] Otomatik ALL modu algilandi (Ana Hub)"
else
    # Eger isim farkliysa, bu sunucu ALT SUNUCUDUR ve merkeze baglanmalidir
    export ENGINE_MODE="gameserver"
    export SERVER_DIR="/server"
    export DATA_DIR="/server/world"
    export PROXY_URL="https://wc-yccy.onrender.com"
    echo "[START] GameServer (Alt Sunucu) modu algilandi — Proxy: $PROXY_URL"
fi

# Bellek tahsisini optimize ederek Render.com sinirlarinda rahatlamasini saglar
export PYTHONMALLOC=malloc

exec python3 /engine.py
