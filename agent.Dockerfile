FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/root
ENV LANG=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

# Temel araçlar + cloudflared
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg lsb-release locales \
    && locale-gen en_US.UTF-8 \
    && curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | gpg --dearmor -o /usr/share/keyrings/cloudflare-main.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
    https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
    > /etc/apt/sources.list.d/cloudflared.list \
    && apt-get update && apt-get install -y cloudflared \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Python + bağımlılıklar
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    net-tools procps \
    && pip3 install --no-cache-dir flask psutil \
    && pip3 cache purge \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent.py /app/agent.py

# Veri dizini
RUN mkdir -p /agent_data/regions /agent_data/chunks /agent_data/backups \
             /agent_data/plugins /agent_data/configs

EXPOSE 5000

CMD ["python3", "/app/agent.py"]
