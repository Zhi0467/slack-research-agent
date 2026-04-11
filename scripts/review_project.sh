#!/usr/bin/env bash
# review_project.sh — run codex review scoped to a project directory.
#
# Recursion guard: codex review discovers .agent/skills/code-review/ and may
# try to invoke this script again. Detect and abort.
if [[ -n "$_REVIEW_PROJECT_RUNNING" ]]; then
    echo "review_project: recursion detected, aborting" >&2
    exit 1
fi
export _REVIEW_PROJECT_RUNNING=1
#
# Usage:
#   review_project.sh <project-slug> [--uncommitted|--base <branch>|--commit <SHA>] [--prompt "<text>"] [--timeout <sec>] [--remote <name>]
#
# Output: review prose captured to a temp file. Path printed as REVIEW_FILE=<path>.
#
# Exit codes:
#   0   — review completed
#   1   — review failed or codex error
#   2   — invalid arguments or project not found
#   3   — branch sync check failed (--base mode only)
#   4   — review timed out (partial output may be in REVIEW_FILE)

set -euo pipefail

# ---- Argument parsing ----

MODE="uncommitted"
BASE_BRANCH=""
COMMIT_SHA=""
PROMPT=""
TIMEOUT_SEC="1200"  # default 20 minutes
REMOTE_NAME=""

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <project-slug> [--uncommitted|--base <branch>|--commit <SHA>] [--prompt \"<text>\"]" >&2
    exit 2
fi

PROJECT_SLUG="$1"
shift

while [[ $# -gt 0 ]]; do
    case "$1" in
        --uncommitted)
            MODE="uncommitted"
            shift
            ;;
        --base)
            MODE="base"
            BASE_BRANCH="$2"
            shift 2
            ;;
        --commit)
            MODE="commit"
            COMMIT_SHA="$2"
            shift 2
            ;;
        --prompt)
            PROMPT="$2"
            shift 2
            ;;
        --timeout)
            TIMEOUT_SEC="$2"
            shift 2
            ;;
        --remote)
            REMOTE_NAME="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

# ---- Resolve project path ----

PROJECT_DIR="${PWD}/projects/${PROJECT_SLUG}"

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "ERROR: project directory not found: $PROJECT_DIR" >&2
    exit 2
fi

# ---- Validate prompt constraints ----

if [[ -n "$PROMPT" ]]; then
    case "$MODE" in
        uncommitted)
            echo "ERROR: --uncommitted cannot combine with --prompt (codex CLI limitation)" >&2
            echo "  Workaround: commit first, then use --commit HEAD --prompt \"...\"" >&2
            exit 2
            ;;
        commit)
            echo "ERROR: --commit cannot combine with --prompt (codex CLI limitation)" >&2
            exit 2
            ;;
        base)
            echo "ERROR: --base cannot combine with --prompt (codex CLI limitation)" >&2
            exit 2
            ;;
    esac
fi

# ---- Branch sync check (--base mode only) ----

if [[ "$MODE" == "base" ]]; then
    echo "review_project: checking branch sync for '${BASE_BRANCH}'..."

    LOCAL_REV=$(git -C "$PROJECT_DIR" rev-parse "$BASE_BRANCH" 2>/dev/null) || {
        echo "ERROR: local branch '${BASE_BRANCH}' not found in ${PROJECT_DIR}" >&2
        exit 3
    }

    # Auto-detect the remote that tracks BASE_BRANCH if --remote not specified.
    # Tries: explicit --remote, upstream tracking remote, upstream, origin.
    if [[ -z "$REMOTE_NAME" ]]; then
        # Check if the local branch has an upstream tracking ref
        _tracking_remote=$(git -C "$PROJECT_DIR" config "branch.${BASE_BRANCH}.remote" 2>/dev/null) || true
        if [[ -n "$_tracking_remote" ]]; then
            REMOTE_NAME="$_tracking_remote"
        elif git -C "$PROJECT_DIR" rev-parse "upstream/${BASE_BRANCH}" >/dev/null 2>&1; then
            REMOTE_NAME="upstream"
        elif git -C "$PROJECT_DIR" rev-parse "origin/${BASE_BRANCH}" >/dev/null 2>&1; then
            REMOTE_NAME="origin"
        fi
    fi

    if [[ -z "$REMOTE_NAME" ]]; then
        echo "WARNING: no remote found for '${BASE_BRANCH}'. Proceeding with local only." >&2
        REMOTE_REV="$LOCAL_REV"
    else
        REMOTE_REV=$(git -C "$PROJECT_DIR" rev-parse "${REMOTE_NAME}/${BASE_BRANCH}" 2>/dev/null) || {
            echo "WARNING: no remote tracking branch '${REMOTE_NAME}/${BASE_BRANCH}'. Proceeding with local only." >&2
            REMOTE_REV="$LOCAL_REV"
        }
    fi

    if [[ "$LOCAL_REV" != "$REMOTE_REV" ]]; then
        echo "ERROR: branch '${BASE_BRANCH}' diverges from '${REMOTE_NAME}/${BASE_BRANCH}'" >&2
        echo "  local:  $LOCAL_REV" >&2
        echo "  remote: $REMOTE_REV" >&2
        echo "  Fix: git -C ${PROJECT_DIR} fetch ${REMOTE_NAME} && git -C ${PROJECT_DIR} merge ${REMOTE_NAME}/${BASE_BRANCH}" >&2
        exit 3
    fi

    echo "review_project: branch sync OK (${REMOTE_NAME}/${BASE_BRANCH} ${LOCAL_REV:0:8})"
fi

# ---- Sanitize PATH ----
# codex review inherits ambient python from PATH, which may point to an
# unrelated venv.  Prefer the system python3 to avoid false import failures.
if [[ -x /usr/bin/python3 ]]; then
    _shim_dir="$(mktemp -d -t codex_review_shims.XXXXXXXX)"
    ln -sf /usr/bin/python3 "${_shim_dir}/python"
    PATH="${_shim_dir}:${PATH}"
    export PATH
fi

# ---- Prepare output capture ----

REVIEW_FILE="$(mktemp -t code_review.XXXXXXXX)"

# ---- Build codex review command ----
# MCP isolation: disable Athena/consult and Slack via -c overrides.
# The reviewer is a pure code reviewer — no external tool access.

CMD=(codex review -c 'mcp_servers.consult.command=""' -c 'mcp_servers.slack.command=""')

case "$MODE" in
    uncommitted)
        CMD+=(--uncommitted)
        ;;
    base)
        CMD+=(--base "$BASE_BRANCH")
        ;;
    commit)
        CMD+=(--commit "$COMMIT_SHA")
        ;;
esac

if [[ -n "$PROMPT" ]]; then
    CMD+=("$PROMPT")
fi

# Note: positional prompts are rejected by all codex review modes
# (--uncommitted, --base, --commit). The _REVIEW_PROJECT_RUNNING env var
# guard already prevents skill-triggered recursion.

# ---- Run review ----

echo "review_project: slug=${PROJECT_SLUG} mode=${MODE} dir=${PROJECT_DIR}"
echo "review_project: command=${CMD[*]}"
if [[ -n "$TIMEOUT_SEC" ]]; then
    echo "review_project: timeout=${TIMEOUT_SEC}s"
fi
echo "review_project: output=${REVIEW_FILE}"

EXIT_CODE=0
if [[ -n "$TIMEOUT_SEC" ]]; then
    # Resolve a portable timeout command: timeout (GNU/Linux) > gtimeout (macOS via coreutils) > python3 fallback
    _timeout_cmd=""
    if command -v timeout >/dev/null 2>&1; then
        _timeout_cmd="timeout"
    elif command -v gtimeout >/dev/null 2>&1; then
        _timeout_cmd="gtimeout"
    fi

    if [[ -n "$_timeout_cmd" ]]; then
        "$_timeout_cmd" "${TIMEOUT_SEC}" bash -c "cd \"$PROJECT_DIR\" && $(printf '%q ' "${CMD[@]}")" > "$REVIEW_FILE" 2>&1 || EXIT_CODE=$?
    else
        # Python fallback for environments without GNU timeout
        _TIMEOUT_SEC="$TIMEOUT_SEC" _REVIEW_FILE="$REVIEW_FILE" \
        python3 -c '
import subprocess, sys, os
try:
    r = subprocess.run(
        sys.argv[1:],
        timeout=int(os.environ["_TIMEOUT_SEC"]),
        stdout=open(os.environ["_REVIEW_FILE"], "w"),
        stderr=subprocess.STDOUT,
    )
    sys.exit(r.returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
' bash -c "cd \"$PROJECT_DIR\" && $(printf '%q ' "${CMD[@]}")" || EXIT_CODE=$?
    fi
    # timeout(1) / gtimeout / python fallback return 124 on timeout; map to exit code 4
    if [[ "$EXIT_CODE" -eq 124 ]]; then
        echo "review_project: TIMED OUT after ${TIMEOUT_SEC}s (partial output may be in REVIEW_FILE)" >&2
        echo ""
        echo "REVIEW_FILE=${REVIEW_FILE}"
        echo "EXIT_CODE=4"
        exit 4
    fi
else
    (cd "$PROJECT_DIR" && "${CMD[@]}") > "$REVIEW_FILE" 2>&1 || EXIT_CODE=$?
fi

echo ""
echo "REVIEW_FILE=${REVIEW_FILE}"
echo "EXIT_CODE=${EXIT_CODE}"
exit $EXIT_CODE
