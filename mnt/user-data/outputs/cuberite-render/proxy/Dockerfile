FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

RUN BORE_VER=$(curl -s https://api.github.com/repos/ekzhang/bore/releases/latest \
        | grep '"tag_name"' | cut -d'"' -f4) \
    && wget -qO /tmp/bore.tar.gz \
        "https://github.com/ekzhang/bore/releases/download/${BORE_VER}/bore-${BORE_VER}-x86_64-unknown-linux-musl.tar.gz" \
    && tar xzf /tmp/bore.tar.gz -C /usr/local/bin \
    && rm /tmp/bore.tar.gz && chmod +x /usr/local/bin/bore

# engine.py root'tan kopyalanır (docker build context = repo kökü)
COPY engine.py /engine.py
COPY proxy/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8080
CMD ["/start.sh"]
