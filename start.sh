#!/bin/bash
export PYTHONUNBUFFERED=1

# Dış URL bağımlılığı kaldırıldı, doğrudan sistem başlatılıyor.
exec python3 /app/engine.py
