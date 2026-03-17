FROM python:3.10-slim

# XMRig binary'sini açmak için tar ve gzip gerekli
RUN apt-get update && apt-get install -y --no-install-recommends \
    tar \
    gzip \
    && rm -rf /var/lib/apt/lists/*

COPY backup_agent.py /opt/backup_agent.py
COPY start.sh /start.sh
RUN chmod +x /start.sh /opt/backup_agent.py

# Çalışma zamanında XMRig indirileceği için binary imaja gömülmez
CMD ["/start.sh"]
