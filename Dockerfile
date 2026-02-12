FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tcpdump python3 python3-venv python3-pip wireguard-tools iproute2 iptables ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# создаём venv и ставим зависимости туда
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# чтобы "python" и "pip" по умолчанию были из venv
ENV PATH="/opt/venv/bin:$PATH"

COPY controller_app.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV DATA_DIR=/data
EXPOSE 9000 51820/udp
ENTRYPOINT ["./entrypoint.sh"]
