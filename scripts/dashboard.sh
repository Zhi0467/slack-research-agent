#!/usr/bin/env bash
# Standalone dashboard publisher. Run in the dedicated 'dashboard' tmux session.
# All config loaded from .env + supervisor_loop.conf by the Python module.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

# Ensure wrangler (npm global bin) is on PATH for Cloudflare Pages deploys
NPM_BIN="$(npm config get prefix 2>/dev/null)/bin"
[[ -d "$NPM_BIN" ]] && export PATH="$PATH:$NPM_BIN"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ "$PYTHON_BIN" = "python3" ] && command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="python3.11"
fi

# Verify minimum Python version (3.9+ required; mirrors supervisor_loop.sh)
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    echo "ERROR: Python 3.9+ required. Found: $("$PYTHON_BIN" --version 2>&1)" >&2
    echo "Set PYTHON_BIN to a supported interpreter." >&2
    exit 1
fi

exec "$PYTHON_BIN" -m src.loop.monitor.dashboard --from-config "$@"
