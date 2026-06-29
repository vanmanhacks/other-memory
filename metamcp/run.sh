#!/usr/bin/env bash
# Other Memory MCP Server launcher
# Usage: hermes mcp add other-memory --command /path/to/metamcp/run.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$REPO_ROOT:$PYTHONPATH"
cd "$REPO_ROOT"
exec python3 -m metamcp.server
