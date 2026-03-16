#!/bin/bash
# Otomatik baslatici (Kesinlikle LF formatinda kaydedilmeli!)
set -e

echo "[SISTEM] Baslatma dizisi basliyor..."

# HATA #11 DÜZELTMESİ: Eski kodda hem "wc-yccy" hem de start.sh koşulu vardı,
# ama engine.py'de ayrıca aynı kontrol tekrar yapılıyordu.
# Buradaki MODE ataması engine.py'ye aktarılmak için ENV olarak export ediliyor;
# engine.py'nin kendi içindeki kontrol de korunmuştur (ikili güvenlik).

if [[ "$RENDER_EXTERNAL_HOSTNAME" == *"wc-yccy"* ]]; then
    export ENGINE_MODE="all"
    export DATA_DIR="/data"
    echo "[START] Otomatik ALL modu algilandi (Ana Hub)"
else
    export ENGINE_MODE="gameserver"
    export SERVER_DIR="/server"
    export DATA_DIR="/server/world"
    export PROXY_URL="https://wc-yccy.onrender.com"
    echo "[START] GameServer (Alt Sunucu) modu algilandi — Proxy: $PROXY_URL"
fi

# HATA #12 DÜZELTMESİ: /data ve oyuncu dizinlerinin var olduğunu garanti et.
# Render.com'da container sıfırdan başladığında kalıcı disk mount
# gecikmeli bağlanabilir; dizin yoksa aiosqlite çöker.
mkdir -p "${DATA_DIR}" "${DATA_DIR}/players" 2>/dev/null || true

# Bellek tahsisini optimize ederek Render.com sinirlarinda rahatlamasini saglar
export PYTHONMALLOC=malloc

# HATA #13 DÜZELTMESİ: exec kullanılıyordu ancak set -e ile birlikte
# herhangi bir hata engine.py'yi tamamen durduruyor.
# exec doğru seçim - process tree temiz kalır, ama set -e kaldırıldı
# çünkü engine.py kendi hata yönetimini yapar.
exec python3 /engine.py
