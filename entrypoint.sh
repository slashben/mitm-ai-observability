#!/usr/bin/env bash
set -e

MITMWEB_PORT="${MITMWEB_PORT:-8081}"
PROXY_PORT="${PROXY_PORT:-8080}"

echo "Starting mitmweb on :${PROXY_PORT} (web UI on :${MITMWEB_PORT})..."
PYTHONUNBUFFERED=1 mitmweb \
    --listen-port "$PROXY_PORT" \
    --web-port "$MITMWEB_PORT" \
    --web-host 0.0.0.0 \
    --set console_eventlog_verbosity=info \
    --no-web-open-browser \
    -s /opt/mitm-addons/ai_contentview.py \
    > /var/log/mitmweb.log 2>&1 &

MITM_PID=$!

for i in $(seq 1 10); do
    sleep 0.5
    if grep -q "Web server listening" /var/log/mitmweb.log 2>/dev/null; then
        break
    fi
done

if ! kill -0 "$MITM_PID" 2>/dev/null; then
    echo "ERROR: mitmweb failed to start"
    cat /var/log/mitmweb.log
    exit 1
fi

WEB_URL=$(grep -oP 'http://\S+' /var/log/mitmweb.log | head -1 | sed 's|0\.0\.0\.0|localhost|')
echo "mitmweb running (pid $MITM_PID)"
echo "Web UI: ${WEB_URL:-http://localhost:${MITMWEB_PORT}}"
echo ""

exec bash
