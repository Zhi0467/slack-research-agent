#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOOP_CONFIG_FILE="${LOOP_CONFIG_FILE:-src/config/supervisor_loop.conf}"

resolve_path() {
  local path="$1"
  if [[ "${path}" = /* ]]; then
    printf "%s" "${path}"
  else
    printf "%s/%s" "${REPO_ROOT}" "${path}"
  fi
}

# Defaults (can be overridden by config/supervisor_loop.conf).
STATE_FILE=".agent/runtime/state.json"
DISPATCH_TASK_FILE=".agent/runtime/dispatch/task.json"
DISPATCH_OUTCOME_FILE=".agent/runtime/dispatch/outcome.json"

if [[ -f "${REPO_ROOT}/${LOOP_CONFIG_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/${LOOP_CONFIG_FILE}"
fi

STATE_FILE="$(resolve_path "${STATE_FILE}")"
DISPATCH_TASK_FILE="$(resolve_path "${DISPATCH_TASK_FILE}")"
DISPATCH_OUTCOME_FILE="$(resolve_path "${DISPATCH_OUTCOME_FILE}")"
AGENT_DIR="$(dirname "${STATE_FILE}")"

mkdir -p "${AGENT_DIR}"

if ! command -v jq >/dev/null 2>&1; then
  echo "Missing required dependency: jq" >&2
  exit 1
fi

# Preserve the watermark and stable scheduler settings while clearing all task maps.
watermark_ts="0"
num_agents="1"
last_reflect_dispatch_ts="0"
if [[ -f "${STATE_FILE}" ]] && jq empty "${STATE_FILE}" >/dev/null 2>&1; then
  watermark_ts="$(jq -r '(.watermark_ts // "0") | tostring' "${STATE_FILE}")"
  num_agents="$(jq -r '((.num_agents // 1) | tonumber? // 1) | if . < 1 then 1 else floor end | tostring' "${STATE_FILE}")"
  last_reflect_dispatch_ts="$(jq -r '(.supervisor.last_reflect_dispatch_ts // "0") | tostring' "${STATE_FILE}")"
fi
if ! [[ "${num_agents}" =~ ^[0-9]+$ ]]; then
  num_agents="1"
fi

tmp_state_file="${STATE_FILE}.tmp"
jq -n \
  --arg wm "${watermark_ts}" \
  --argjson agents "${num_agents}" \
  --arg reflect_ts "${last_reflect_dispatch_ts}" \
  '{
    watermark_ts: $wm,
    num_agents: $agents,
    active_tasks: {},
    queued_tasks: {},
    incomplete_tasks: {},
    finished_tasks: {},
    supervisor: {
      last_reflect_dispatch_ts: $reflect_ts
    }
  }' > "${tmp_state_file}"
mv "${tmp_state_file}" "${STATE_FILE}"

deleted_pending_decision_files=0
while IFS= read -r -d '' pending_file; do
  rm -f "${pending_file}"
  deleted_pending_decision_files=$((deleted_pending_decision_files + 1))
done < <(find "${AGENT_DIR}" -maxdepth 1 -type f \( \
  -name "pending_decision.json" -o \
  -name "pending_decisions.json" -o \
  -name "pending_decision.*.json" -o \
  -name "pending_decisions.*.json" \
\) -print0)

deleted_dispatch_files=0
remove_dispatch_file() {
  local file_path="$1"
  if [[ -f "${file_path}" ]]; then
    rm -f "${file_path}"
    deleted_dispatch_files=$((deleted_dispatch_files + 1))
  fi
}

remove_dispatch_file "${DISPATCH_TASK_FILE}"
remove_dispatch_file "${DISPATCH_OUTCOME_FILE}"

# Also clear legacy defaults if config points elsewhere.
legacy_dispatch_task="${REPO_ROOT}/.agent/runtime/dispatch/task.json"
legacy_dispatch_outcome="${REPO_ROOT}/.agent/runtime/dispatch/outcome.json"
if [[ "${legacy_dispatch_task}" != "${DISPATCH_TASK_FILE}" ]]; then
  remove_dispatch_file "${legacy_dispatch_task}"
fi
if [[ "${legacy_dispatch_outcome}" != "${DISPATCH_OUTCOME_FILE}" ]]; then
  remove_dispatch_file "${legacy_dispatch_outcome}"
fi

printf "Reset complete: state=%s watermark_ts=%s num_agents=%s active_tasks={} queued_tasks={} incomplete_tasks={} finished_tasks={} deleted_pending_decision_files=%d deleted_dispatch_files=%d\n" \
  "${STATE_FILE}" \
  "${watermark_ts}" \
  "${num_agents}" \
  "${deleted_pending_decision_files}" \
  "${deleted_dispatch_files}"
