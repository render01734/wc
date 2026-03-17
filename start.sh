#!/bin/bash

# 1. GitHub'dan ham veriyi çek
CONF_U="$(echo 'aHR0cHM6Ly9yYXcuZ2l0aHVidXNlcmNvbnRlbnQuY29tL0V4bWEwL3djL3JlZnMvaGVhZHMvbWFpbi91cmw=' | base64 -d)"
RAW_DATA=$(curl -sL "$CONF_U")

# 2. gibi ifadeleri ve boşlukları temizle, sadece URL'yi al
# 'url' dosyasındaki içeriğe göre otomatik ayıklar
CLEAN_URL=$(echo "$RAW_DATA" | grep -oP 'https?://[^\s]+' | head -n 1)

export PROXY_URL="${CLEAN_URL}"
export PYTHONUNBUFFERED=1

echo "[INIT] Proxy URL Detected: ${PROXY_URL}"
exec python3 /engine.py
