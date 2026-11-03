#!/usr/bin/env bash
# Other Memory MetaMCP bridge launcher — works from any checkout location.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load secrets + optional overrides (SEARXNG_URL, REDIS_URL, …) from .env.
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

# Prefer the project venv if present; otherwise fall back to system python3.
PYTHON="$ROOT/metamcp/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

cd "$ROOT"
exec "$PYTHON" -m metamcp.server
