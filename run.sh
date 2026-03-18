#!/usr/bin/env bash
# Run the mitm-ai-observability container.
#
# Usage:
#   ANTHROPIC_API_KEY=sk-... ./run.sh
#
# Or pass the key inline:
#   ./run.sh -e ANTHROPIC_API_KEY=sk-...

IMAGE_NAME="hisu/mitm-ai-observability"

OPENCLAW_MOUNTS=""
if [ -d "$HOME/.openclaw" ]; then
    OPENCLAW_MOUNTS="-v $HOME/.openclaw:/root/.openclaw:z"
    echo "Detected ~/.openclaw — mounting OpenClaw config into container"
fi

docker run -it --rm \
    ${ANTHROPIC_API_KEY:+-e ANTHROPIC_API_KEY} \
    ${OPENCLAW_GATEWAY_TOKEN:+-e OPENCLAW_GATEWAY_TOKEN} \
    -p 8080:8080 \
    -p 8081:8081 \
    -v "$HOME":/workspace:z \
    -v "$HOME/.claude":/root/.claude:z \
    -v "$HOME/.claude.json":/root/.claude.json:z \
    $OPENCLAW_MOUNTS \
    "$IMAGE_NAME" \
    "$@"
