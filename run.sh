#!/usr/bin/env bash
# Run the mitm-ai-observability container.
#
# Usage:
#   ANTHROPIC_API_KEY=sk-... ./run.sh
#
# Or pass the key inline:
#   ./run.sh -e ANTHROPIC_API_KEY=sk-...

IMAGE_NAME="hisu/mitm-ai-observability"

docker run -it --rm \
    ${ANTHROPIC_API_KEY:+-e ANTHROPIC_API_KEY} \
    -p 8081:8081 \
    -v "$HOME":/workspace:z \
    -v "$HOME/.claude":/root/.claude:z \
    -v "$HOME/.claude.json":/root/.claude.json:z \
    "$IMAGE_NAME" \
    "$@"
