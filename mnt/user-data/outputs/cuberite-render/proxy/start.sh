#!/bin/bash
# Proxy başlatıcı — engine.py proxy modunda
export ENGINE_MODE=proxy
export DATA_DIR=/data
exec python3 /engine.py
