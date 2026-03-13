FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    procps util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
    flask flask-socketio eventlet psutil

WORKDIR /app

# Normal agent modu
COPY agent.py     ./agent.py

# IS_PANEL=1 modu için (panel bu dosyaları başlatır)
COPY mc_panel.py  ./mc_panel.py
COPY cluster.py   ./cluster.py

# Dizinler — cuberite_cache Cuberite C++ uyumlu
RUN mkdir -p \
    /agent_data/regions/world \
    /agent_data/regions/world_nether \
    /agent_data/regions/world_the_end \
    /agent_data/backups \
    /agent_data/plugins \
    /agent_data/configs \
    /agent_data/chunks \
    /agent_data/cuberite_cache

EXPOSE 8080 5000

# IS_PANEL=0 → agent (PORT=8080)
# IS_PANEL=1 → panel host: mc_panel.py WORKER_URL ile (PORT=5000)
CMD ["python3", "/app/agent.py"]
