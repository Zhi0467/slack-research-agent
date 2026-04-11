#!/usr/bin/env bash
# project_worker.sh — spawn a codex sub-session scoped to a project directory.
#
# Usage:
#   scripts/project_worker.sh <project-slug> <prompt-file> [--timeout <seconds>]
#
# The sub-session inherits the project's AGENTS.md/CLAUDE.md (loaded from CWD),
# has access to consult (Athena) but NOT Slack, and runs with full autonomy.
#
# Exit codes:
#   0   — sub-session completed successfully
#   1   — sub-session failed
#   2   — invalid arguments or project not found
#   124 — sub-session timed out

set -euo pipefail

# ---- Argument parsing ----

TIMEOUT=14400  # 4 hours default

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <project-slug> <prompt-file> [--timeout <seconds>]" >&2
    exit 2
fi

PROJECT_SLUG="$1"
PROMPT_FILE="$2"
shift 2

while [[ $# -gt 0 ]]; do
    case "$1" in
        --timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

# ---- Resolve project path ----

# Works from both main repo and worktrees: use script's own location
# to find repo root, then resolve project relative to CWD (which may be
# a worktree).
PROJECT_DIR="${PWD}/projects/${PROJECT_SLUG}"

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "ERROR: project directory not found: $PROJECT_DIR" >&2
    exit 2
fi

if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "ERROR: prompt file not found: $PROMPT_FILE" >&2
    exit 2
fi

# ---- Prepare output capture ----

RESULTS_FILE="$(mktemp -t project_worker.XXXXXXXX)"

# ---- Strip Slack env vars (defense-in-depth) ----

unset SLACK_BOT_TOKEN 2>/dev/null || true
unset SLACK_APP_TOKEN 2>/dev/null || true
unset SLACK_MCP_ENABLED_TOOLS 2>/dev/null || true

# ---- Run sub-session ----

echo "project_worker: slug=${PROJECT_SLUG} dir=${PROJECT_DIR} timeout=${TIMEOUT}s"
echo "project_worker: prompt_file=${PROMPT_FILE} results_file=${RESULTS_FILE}"

EXIT_CODE=0
timeout "${TIMEOUT}" codex exec \
    --yolo \
    --ephemeral \
    --skip-git-repo-check \
    -C "${PROJECT_DIR}" \
    - < "${PROMPT_FILE}" \
    2>&1 | tee "${RESULTS_FILE}" | tail -200 \
    || EXIT_CODE=$?

# timeout returns 124 on expiry
if [[ $EXIT_CODE -eq 124 ]]; then
    echo "project_worker: TIMED OUT after ${TIMEOUT}s" >&2
fi

echo ""
echo "RESULTS_FILE=${RESULTS_FILE}"
echo "EXIT_CODE=${EXIT_CODE}"
exit $EXIT_CODE
