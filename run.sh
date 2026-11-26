#!/usr/bin/env bash
# Start the booking agent and open the chat UI.  Usage: ./run.sh   (PORT=8000 by default)
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed." >&2
  echo "  install it: curl -LsSf https://astral.sh/uv/install.sh | sh   (or: brew install uv)" >&2
  exit 1
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "error: ANTHROPIC_API_KEY is not set." >&2
  echo "  get a key at https://console.anthropic.com, then: export ANTHROPIC_API_KEY=..." >&2
  exit 1
fi

echo "installing dependencies…"
uv sync --quiet

PORT="${PORT:-8000}"
URL="http://127.0.0.1:${PORT}"

# Open the browser once the server answers, in the background.
(
  for _ in $(seq 1 50); do
    if curl -fsS -o /dev/null "$URL/" 2>/dev/null; then
      if command -v open >/dev/null 2>&1; then open "$URL"
      elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1
      fi
      exit 0
    fi
    sleep 0.2
  done
) &

echo "chat UI: $URL   (model: ${ORANGEINVEST_MODEL:-claude-sonnet-5-5}, Ctrl-C to stop)"
exec uv run uvicorn app.api:create_app --factory --host 127.0.0.1 --port "$PORT" --log-level warning
