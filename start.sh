#!/bin/bash
export PYTHONUNBUFFERED=1

echo "[SİSTEM] Tüm limitler kaldırıldı. Doğrudan madenci başlatılıyor..."
exec python3 /app/engine.py
