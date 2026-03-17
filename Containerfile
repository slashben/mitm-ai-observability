FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir mitmproxy anthropic

RUN npm install -g @anthropic-ai/claude-code

# Generate mitmproxy CA certificate and install it into the system trust store
# mitmdump needs extra time on emulated architectures (e.g. arm64 via QEMU)
RUN mitmdump &>/dev/null & \
    for i in $(seq 120); do test -f /root/.mitmproxy/mitmproxy-ca-cert.pem && break || sleep 1; done \
    && cp /root/.mitmproxy/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/mitmproxy.crt \
    && update-ca-certificates

ENV HTTP_PROXY=http://127.0.0.1:8080 \
    HTTPS_PROXY=http://127.0.0.1:8080 \
    NODE_EXTRA_CA_CERTS=/root/.mitmproxy/mitmproxy-ca-cert.pem

EXPOSE 8080 8081

COPY addons/ /opt/mitm-addons/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace

ENTRYPOINT ["/entrypoint.sh"]
