#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "Not inside a git repository."
  exit 1
fi

if [[ "$(git rev-parse --show-toplevel)" != "$ROOT" ]]; then
  echo "Run this script from the root repository."
  exit 1
fi

LOCAL_SUBMODULE_REMOTE_DIR="${LOCAL_SUBMODULE_REMOTE_DIR:-$HOME/.git-submodule-remotes}"

ensure_repo_exists() {
  local path="$1"
  if [[ ! -d "$path/.git" ]]; then
    echo "Missing nested git repo: $path"
    exit 1
  fi
}

ensure_no_nested_git() {
  local path="$1"
  local nested
  nested="$(find "$path" -mindepth 2 -name .git -print | sed -n '1,1p' || true)"
  if [[ -n "$nested" ]]; then
    echo "Nested git repo detected under $path: $nested"
    echo "Enforce one repo layer per path; absorb/remove nested repos first."
    exit 1
  fi
}

ensure_initial_commit() {
  local path="$1"
  if git -C "$path" rev-parse --verify HEAD >/dev/null 2>&1; then
    return 0
  fi
  echo "Creating initial commit in $path"
  git -C "$path" add -A
  if git -C "$path" diff --cached --quiet; then
    git -C "$path" commit --allow-empty -m "chore: initial commit for submodule registration"
  else
    git -C "$path" commit -m "chore: initial commit for submodule registration"
  fi
}

pick_url() {
  local path="$1"
  local override="$2"
  local url
  url="$(git -C "$path" remote get-url origin 2>/dev/null || true)"
  if [[ -n "$url" ]]; then
    echo "$url"
    return 0
  fi
  if [[ -n "$override" ]]; then
    echo "$override"
    return 0
  fi
  local remote_name
  remote_name="$(echo "$path" | tr '/' '-')"
  local bare_repo="$LOCAL_SUBMODULE_REMOTE_DIR/$remote_name.git"
  mkdir -p "$LOCAL_SUBMODULE_REMOTE_DIR"
  if [[ ! -d "$bare_repo" ]]; then
    git init --bare "$bare_repo" >/dev/null
  fi
  if ! git -C "$path" remote get-url origin >/dev/null 2>&1; then
    git -C "$path" remote add origin "$bare_repo"
  fi
  local branch
  branch="$(git -C "$path" rev-parse --abbrev-ref HEAD)"
  git -C "$path" push -u origin "$branch" >/dev/null
  echo "file://$bare_repo"
}

register_submodule() {
  local path="$1"
  local url="$2"
  echo "Registering submodule: $path"
  git -c protocol.file.allow=always submodule add -f "$url" "$path"
}

ensure_repo_exists "mcp/slack-mcp-server"

ensure_no_nested_git "mcp/slack-mcp-server"

MCP_SLACK_URL="$(pick_url "mcp/slack-mcp-server" "")"

register_submodule "mcp/slack-mcp-server" "$MCP_SLACK_URL"

git submodule sync --recursive

echo
echo "Submodule registration complete."
echo "Next steps:"
echo "1) Review .gitmodules"
echo "2) git add .gitmodules mcp/slack-mcp-server"
echo "3) git commit -m \"chore: register the public slack MCP submodule\""
