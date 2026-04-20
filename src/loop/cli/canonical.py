"""Canonical local configuration for Murphy.

The canonical config lives at repo-root ``config.toml`` and is the source of
truth for local operator intent. Generated files such as ``.env``,
``.codex/config.toml``, ``src/config/claude_mcp.json``, and
``slack-app-manifest.json`` are projections rendered from this model.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # Python 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 fallback
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _tomllib = None  # type: ignore[assignment]

CANONICAL_CONFIG_PATH = Path("config.toml")
LEGACY_CANONICAL_CONFIG_PATH = Path(".murphy/config.toml")
DEFAULT_MANIFEST_PATH = Path("slack-app-manifest.json")

DEFAULT_AGENT_NAME = "Murphy"
DEFAULT_SLACK_APP_NAME = "Murphy Agent"
DEFAULT_SLACK_APP_DESCRIPTION = "Self-hosted Slack supervisor for long-running AI work"
DEFAULT_CHATGPT_PROJECT = "Murphy"
DEFAULT_WORKER_BACKEND = "codex"
DEFAULT_WORKER_COMMAND = "codex exec --yolo --ephemeral --skip-git-repo-check -"
DEFAULT_DEV_REVIEW_BACKEND = "claude"
DEFAULT_DEV_REVIEW_COMMAND = (
    "claude -p --dangerously-skip-permissions --mcp-config src/config/claude_mcp.json"
)
DEFAULT_TRIBUNE_COMMAND = "gemini -m gemini-3.1-pro-preview -p '' -y --output-format text"
DEFAULT_TRIBUNE_FALLBACK_MODELS = ["gemini-3-flash", "gemini-2.5-flash"]
DEFAULT_MEMORY_BODY = (
    "# Curated Memory\n\n- Add durable preferences, constraints, and operating notes here.\n"
)
DEFAULT_GOALS_BODY = "# Long-Term Goals\n\n- Add active goals and progress notes here.\n"

SLACK_MCP_ENABLED_TOOLS = [
    "conversations_search_messages",
    "conversations_replies",
    "conversations_add_message",
    "reactions_add",
    "reactions_remove",
    "attachment_get_data",
    "attachment_upload",
]

SLACK_MCP_ENABLED_TOOLS_ENV = ",".join(SLACK_MCP_ENABLED_TOOLS)

CLAUDE_MCP_ENABLED_TOOLS_ENV = ",".join(
    [
        "conversations_replies",
        "conversations_add_message",
        "reactions_add",
        "reactions_remove",
        "attachment_get_data",
        "attachment_upload",
    ]
)

USER_SCOPES = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "mpim:write",
    "users:read",
    "chat:write",
    "search:read",
    "files:read",
    "files:write",
    "reactions:read",
    "reactions:write",
]


@dataclass
class AgentSection:
    name: str = DEFAULT_AGENT_NAME


@dataclass
class SlackSection:
    app_name: str = DEFAULT_SLACK_APP_NAME
    app_description: str = DEFAULT_SLACK_APP_DESCRIPTION
    user_token: str = ""
    default_channel_id: str = ""
    agent_user_id: str = ""


@dataclass
class RuntimeSection:
    max_concurrent_workers: int = 1
    session_minutes: int = 360


@dataclass
class WorkerSection:
    backend: str = DEFAULT_WORKER_BACKEND
    command: str = DEFAULT_WORKER_COMMAND
    model: str = "gpt-5.4"
    reasoning_effort: str = "high"
    personality: str = "pragmatic"
    approval_policy: str = "never"
    sandbox_mode: str = "danger-full-access"
    web_search: str = "live"
    chatgpt_project: str = DEFAULT_CHATGPT_PROJECT


@dataclass
class ConsultSection:
    command: str = ""
    args: List[str] = field(default_factory=list)


@dataclass
class DeveloperReviewSection:
    enabled: bool = True
    backend: str = DEFAULT_DEV_REVIEW_BACKEND
    command: str = DEFAULT_DEV_REVIEW_COMMAND


@dataclass
class TribuneSection:
    enabled: bool = False
    command: str = DEFAULT_TRIBUNE_COMMAND
    fallback_models: List[str] = field(default_factory=lambda: list(DEFAULT_TRIBUNE_FALLBACK_MODELS))
    review_rounds: int = 0
    maintenance_rounds: int = 0


@dataclass
class DashboardSection:
    export_enabled: bool = False
    export_dir: str = "dashboard-export"
    git_push: bool = False
    git_remote: str = "origin"
    git_branch: str = "deploy"
    cf_pages_enabled: bool = False
    cf_pages_project: str = ""
    cf_pages_dir: str = ".agent/runtime/public-site"
    cf_pages_interval: int = 900
    gpu_monitor: bool = False
    gpu_node_alias: str = ""
    gpu_command_timeout: int = 4


@dataclass
class FilesSection:
    manifest_path: str = str(DEFAULT_MANIFEST_PATH)


@dataclass
class CanonicalConfig:
    """Top-level canonical config rendered to/from repo-root ``config.toml``."""

    agent: AgentSection = field(default_factory=AgentSection)
    slack: SlackSection = field(default_factory=SlackSection)
    runtime: RuntimeSection = field(default_factory=RuntimeSection)
    worker: WorkerSection = field(default_factory=WorkerSection)
    consult: ConsultSection = field(default_factory=ConsultSection)
    developer_review: DeveloperReviewSection = field(default_factory=DeveloperReviewSection)
    tribune: TribuneSection = field(default_factory=TribuneSection)
    dashboard: DashboardSection = field(default_factory=DashboardSection)
    files: FilesSection = field(default_factory=FilesSection)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def get(self, dotted_key: str) -> Any:
        section, _, leaf = dotted_key.partition(".")
        if not leaf:
            raise KeyError(f"config key must be '<section>.<field>': {dotted_key!r}")
        sect = getattr(self, section, None)
        if sect is None or not is_dataclass(sect):
            raise KeyError(f"unknown section: {section!r}")
        if not hasattr(sect, leaf):
            raise KeyError(f"unknown field: {dotted_key!r}")
        return getattr(sect, leaf)

    def set(self, dotted_key: str, value: Any) -> None:
        section, _, leaf = dotted_key.partition(".")
        if not leaf:
            raise KeyError(f"config key must be '<section>.<field>': {dotted_key!r}")
        sect = getattr(self, section, None)
        if sect is None or not is_dataclass(sect):
            raise KeyError(f"unknown section: {section!r}")
        if not hasattr(sect, leaf):
            raise KeyError(f"unknown field: {dotted_key!r}")
        current = getattr(sect, leaf)
        setattr(sect, leaf, _coerce_value(value, current))

    def unset(self, dotted_key: str) -> None:
        section, _, leaf = dotted_key.partition(".")
        if not leaf:
            raise KeyError(f"config key must be '<section>.<field>': {dotted_key!r}")
        sect = getattr(self, section, None)
        if sect is None or not is_dataclass(sect):
            raise KeyError(f"unknown section: {section!r}")
        for f in fields(sect):
            if f.name != leaf:
                continue
            if f.default is not MISSING:
                setattr(sect, leaf, f.default)
                return
            if f.default_factory is not MISSING:  # type: ignore[comparison-overlap]
                setattr(sect, leaf, f.default_factory())  # type: ignore[misc]
                return
        raise KeyError(f"unknown field: {dotted_key!r}")

    def dotted_keys(self) -> List[str]:
        keys: List[str] = []
        for f in fields(self):
            sect = getattr(self, f.name)
            if not is_dataclass(sect):
                continue
            for sf in fields(sect):
                keys.append(f"{f.name}.{sf.name}")
        return sorted(keys)


@dataclass
class ProjectionResult:
    status: str
    relative_path: str


@dataclass
class ImportConflict:
    key: str
    values: Dict[str, Any]


@dataclass
class ImportResult:
    config: CanonicalConfig
    warnings: List[str] = field(default_factory=list)
    conflicts: List[ImportConflict] = field(default_factory=list)
    imported_keys: List[str] = field(default_factory=list)


@dataclass
class DoctorFinding:
    level: str
    code: str
    message: str


def _coerce_value(value: Any, reference: Any) -> Any:
    if isinstance(reference, bool):
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError(f"expected boolean, got {value!r}")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, list):
        if isinstance(value, list):
            return [str(item) for item in value]
        text = str(value).strip()
        if not text:
            return []
        return [part.strip() for part in text.split(",") if part.strip()]
    return str(value)


_SECTION_RE = re.compile(
    r"^\[([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\]\s*$"
)
_KEY_VAL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")


def _strip_inline_comment(raw: str) -> str:
    in_str = False
    escape = False
    for idx, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if ch == "#" and not in_str:
            return raw[:idx].rstrip()
    return raw.rstrip()


def _unescape_string(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    if text.startswith('"') and text.endswith('"'):
        return _unescape_string(text[1:-1])
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items: List[str] = []
        current: List[str] = []
        in_str = False
        escape = False
        for ch in inner:
            if escape:
                current.append(ch)
                escape = False
                continue
            if ch == "\\" and in_str:
                current.append(ch)
                escape = True
                continue
            if ch == '"':
                current.append(ch)
                in_str = not in_str
                continue
            if ch == "," and not in_str:
                items.append("".join(current).strip())
                current = []
                continue
            current.append(ch)
        if current:
            items.append("".join(current).strip())
        return [_parse_scalar(item) for item in items if item]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if "." not in text:
            return int(text)
        return float(text)
    except ValueError:
        return text


def parse_toml_like(text: str) -> Dict[str, Any]:
    """Parse a narrow TOML subset used by Murphy config files.

    When ``tomllib`` (Python 3.11+) or ``tomli`` is available it is used so
    full TOML semantics are honored. Otherwise a narrow built-in fallback
    handles string/bool/int/float scalars, string/int/bool arrays, single-level
    keys, and dotted table headers such as ``[mcp_servers.slack.env]``.
    """

    if _tomllib is not None:
        return _tomllib.loads(text)

    data: Dict[str, Any] = {}
    current: Dict[str, Any] = data
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            current = data
            for part in section_match.group(1).split("."):
                current = current.setdefault(part, {})
            continue
        key_match = _KEY_VAL_RE.match(line)
        if not key_match:
            continue
        key = key_match.group(1)
        current[key] = _parse_scalar(_strip_inline_comment(key_match.group(2)))
    return data


def _escape_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _format_toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format_toml_scalar(item) for item in value) + "]"
    return f'"{_escape_string(str(value))}"'


def dump_canonical_toml(cfg: CanonicalConfig) -> str:
    chunks = [
        "# Murphy canonical local config.\n",
        "# This file is the source of truth for user-editable settings.\n",
        "# Generated projections are rendered from here by `murphy config sync`\n",
        "# and automatically refreshed by `murphy init`.\n\n",
    ]
    for f in fields(cfg):
        section = getattr(cfg, f.name)
        if not is_dataclass(section):
            continue
        chunks.append(f"[{f.name}]\n")
        for sf in fields(section):
            chunks.append(f"{sf.name} = {_format_toml_scalar(getattr(section, sf.name))}\n")
        chunks.append("\n")
    return "".join(chunks).rstrip() + "\n"


def _apply_parsed(cfg: CanonicalConfig, parsed: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    for section_name, raw_section in parsed.items():
        section = getattr(cfg, section_name, None)
        if section is None or not is_dataclass(section):
            warnings.append(f"unknown section ignored: [{section_name}]")
            continue
        if not isinstance(raw_section, dict):
            warnings.append(f"section [{section_name}] must contain key/value pairs")
            continue
        known_fields = {f.name for f in fields(section)}
        for key, value in raw_section.items():
            if key not in known_fields:
                warnings.append(f"unknown field ignored: {section_name}.{key}")
                continue
            try:
                coerced = _coerce_value(value, getattr(section, key))
            except (TypeError, ValueError) as exc:
                warnings.append(f"bad value for {section_name}.{key}: {exc}")
                continue
            setattr(section, key, coerced)
    return warnings


def canonical_path(repo_root: Path) -> Path:
    return repo_root / CANONICAL_CONFIG_PATH


def existing_canonical_path(repo_root: Path) -> Optional[Path]:
    primary = canonical_path(repo_root)
    if primary.exists():
        return primary
    legacy = repo_root / LEGACY_CANONICAL_CONFIG_PATH
    if legacy.exists():
        return legacy
    return None


def load_canonical(path: Path) -> Tuple[CanonicalConfig, List[str]]:
    cfg = CanonicalConfig()
    if not path.exists():
        return cfg, []
    parsed = parse_toml_like(path.read_text(encoding="utf-8"))
    warnings = _apply_parsed(cfg, parsed)
    return cfg, warnings


def save_canonical(cfg: CanonicalConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_canonical_toml(cfg), encoding="utf-8")


def infer_agent_name_from_app_name(app_name: str) -> Optional[str]:
    candidate = str(app_name or "").strip()
    if not candidate or " " in candidate:
        return None
    return candidate


def _repo_path(repo_root: Path, relative: str) -> str:
    return (repo_root / relative).resolve().as_posix()


def _slack_mcp_command(repo_root: Path) -> str:
    return _repo_path(repo_root, "mcp/slack-mcp-server/build/slack-mcp-server")


def _env_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _env_line(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{key}={'true' if value else 'false'}"
    if isinstance(value, int):
        return f"{key}={value}"
    if isinstance(value, list):
        return f"{key}={','.join(str(item) for item in value)}"
    text = str(value)
    if not text:
        return f'{key}=""'
    if re.search(r"\s", text):
        return f"{key}={_env_quote(text)}"
    return f"{key}={text}"


def render_env(cfg: CanonicalConfig) -> str:
    lines = [
        "# Murphy Agent environment",
        "# Generated from config.toml by `murphy config sync`.",
        "",
        "# Required: Slack token used by the supervisor itself.",
        _env_line("SLACK_USER_TOKEN", cfg.slack.user_token or "xoxp-your-token-here"),
        "",
        "# Required for system messages such as maintenance summaries.",
        _env_line("DEFAULT_CHANNEL_ID", cfg.slack.default_channel_id or "C0YOUR_CHANNEL_ID"),
        "",
        "# Public-facing agent name used in worker/reviewer prompts.",
        _env_line("AGENT_NAME", cfg.agent.name),
    ]
    if cfg.slack.agent_user_id:
        lines.extend(
            [
                "",
                "# Optional explicit Slack user ID override for the agent account.",
                _env_line("AGENT_USER_ID", cfg.slack.agent_user_id),
            ]
        )
    lines.extend(
        [
            "",
            "# Worker CLI used for regular task execution.",
            _env_line("WORKER_CMD", cfg.worker.command),
            "",
            "# Developer-review CLI used during maintenance phase 1.",
            _env_line(
                "DEV_REVIEW_CMD",
                cfg.developer_review.command if cfg.developer_review.enabled else "",
            ),
            "",
            "# Optional parallelism and runtime sizing.",
            _env_line("MAX_CONCURRENT_WORKERS", cfg.runtime.max_concurrent_workers),
            _env_line("SESSION_MINUTES", cfg.runtime.session_minutes),
            "",
            "# Optional Tribune review.",
            _env_line("TRIBUNE_CMD", cfg.tribune.command),
            _env_line("TRIBUNE_FALLBACK_MODELS", cfg.tribune.fallback_models),
            _env_line(
                "TRIBUNE_MAX_REVIEW_ROUNDS",
                cfg.tribune.review_rounds if cfg.tribune.enabled else 0,
            ),
            _env_line(
                "TRIBUNE_MAINT_ROUNDS",
                cfg.tribune.maintenance_rounds if cfg.tribune.enabled else 0,
            ),
            "",
            "# Optional dashboard/static export.",
            _env_line("DASHBOARD_EXPORT_ENABLED", cfg.dashboard.export_enabled),
            _env_line("DASHBOARD_EXPORT_DIR", cfg.dashboard.export_dir),
            _env_line("DASHBOARD_GIT_PUSH", cfg.dashboard.git_push),
            _env_line("DASHBOARD_GIT_REMOTE", cfg.dashboard.git_remote),
            _env_line("DASHBOARD_GIT_BRANCH", cfg.dashboard.git_branch),
            _env_line("DASHBOARD_CF_PAGES_ENABLED", cfg.dashboard.cf_pages_enabled),
            _env_line("DASHBOARD_CF_PAGES_PROJECT", cfg.dashboard.cf_pages_project),
            _env_line("DASHBOARD_CF_PAGES_DIR", cfg.dashboard.cf_pages_dir),
            _env_line("DASHBOARD_CF_PAGES_INTERVAL", cfg.dashboard.cf_pages_interval),
            _env_line("DASHBOARD_GPU_MONITOR", cfg.dashboard.gpu_monitor),
            _env_line("DASHBOARD_GPU_NODE_ALIAS", cfg.dashboard.gpu_node_alias),
            _env_line("DASHBOARD_GPU_COMMAND_TIMEOUT", cfg.dashboard.gpu_command_timeout),
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_codex_config(cfg: CanonicalConfig, repo_root: Path) -> str:
    lines = [
        "# Codex worker configuration for Murphy Agent.",
        "# Generated from config.toml by `murphy config sync`.",
        "",
        f"model = {_format_toml_scalar(cfg.worker.model)}",
        f"model_reasoning_effort = {_format_toml_scalar(cfg.worker.reasoning_effort)}",
        f"personality = {_format_toml_scalar(cfg.worker.personality)}",
        "",
        f"approval_policy = {_format_toml_scalar(cfg.worker.approval_policy)}",
        f"sandbox_mode = {_format_toml_scalar(cfg.worker.sandbox_mode)}",
        f"web_search = {_format_toml_scalar(cfg.worker.web_search)}",
        "",
        "[sandbox_workspace_write]",
        "network_access = true",
        "",
        "# Optional consult server. Leave command empty to disable it.",
        "[mcp_servers.consult]",
        f"command = {_format_toml_scalar(cfg.consult.command)}",
        f"args = {_format_toml_scalar(cfg.consult.args)}",
        "tool_timeout_sec = 7200",
        "",
        "[mcp_servers.consult.env]",
        f"CHATGPT_DEFAULT_PROJECT = {_format_toml_scalar(cfg.worker.chatgpt_project)}",
        "",
        "[mcp_servers.slack]",
        f"command = {_format_toml_scalar(_slack_mcp_command(repo_root))}",
        'args = ["--transport", "stdio"]',
        "tool_timeout_sec = 180",
        "",
        "enabled_tools = [",
    ]
    for tool_name in SLACK_MCP_ENABLED_TOOLS:
        lines.append(f"  {_format_toml_scalar(tool_name)},")
    lines.extend(
        [
            "]",
            "",
            "[mcp_servers.slack.env]",
            f"SLACK_MCP_XOXP_TOKEN = {_format_toml_scalar(cfg.slack.user_token or 'xoxp-paste-your-token-here')}",
            'SLACK_MCP_ADD_MESSAGE_TOOL = "true"',
            'SLACK_MCP_ADD_MESSAGE_MARK = "true"',
            f"SLACK_MCP_ENABLED_TOOLS = {_format_toml_scalar(SLACK_MCP_ENABLED_TOOLS_ENV)}",
            "",
            "[features]",
            "multi_agent = true",
            "prevent_idle_sleep = true",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_claude_config(cfg: CanonicalConfig, repo_root: Path) -> str:
    payload = {
        "mcpServers": {
            "slack": {
                "command": _slack_mcp_command(repo_root),
                "args": ["--transport", "stdio"],
                "env": {
                    "SLACK_MCP_XOXP_TOKEN": cfg.slack.user_token or "<your-slack-xoxp-token>",
                    "SLACK_MCP_ADD_MESSAGE_TOOL": "true",
                    "SLACK_MCP_ADD_MESSAGE_MARK": "true",
                    "SLACK_MCP_ENABLED_TOOLS": CLAUDE_MCP_ENABLED_TOOLS_ENV,
                },
            }
        }
    }
    return json.dumps(payload, indent=2) + "\n"


def render_manifest(cfg: CanonicalConfig) -> str:
    manifest = {
        "display_information": {
            "name": cfg.slack.app_name,
            "description": cfg.slack.app_description,
        },
        "oauth_config": {
            "scopes": {
                "user": USER_SCOPES,
            }
        },
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
    return json.dumps(manifest, indent=2) + "\n"


def build_projection_map(cfg: CanonicalConfig, repo_root: Path) -> Dict[Path, str]:
    manifest_path = repo_root / Path(cfg.files.manifest_path)
    return {
        repo_root / Path(".env"): render_env(cfg),
        repo_root / Path(".codex/config.toml"): render_codex_config(cfg, repo_root),
        repo_root / Path("src/config/claude_mcp.json"): render_claude_config(cfg, repo_root),
        manifest_path: render_manifest(cfg),
    }


def write_text_file(path: Path, content: str, *, force: bool) -> str:
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return "unchanged"
        if not force:
            return "skipped"
        status = "updated"
    else:
        status = "created"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return status


def _write_if_missing(path: Path, content: str) -> str:
    if path.exists():
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "created"


def sync_projections(cfg: CanonicalConfig, repo_root: Path, *, force: bool) -> List[ProjectionResult]:
    results: List[ProjectionResult] = []
    for path, content in build_projection_map(cfg, repo_root).items():
        status = write_text_file(path, content, force=force)
        results.append(ProjectionResult(status=status, relative_path=str(path.relative_to(repo_root))))
    memory_path = repo_root / Path(".agent/memory/memory.md")
    goals_path = repo_root / Path(".agent/memory/long_term_goals.md")
    results.append(
        ProjectionResult(
            status=_write_if_missing(memory_path, DEFAULT_MEMORY_BODY),
            relative_path=str(memory_path.relative_to(repo_root)),
        )
    )
    results.append(
        ProjectionResult(
            status=_write_if_missing(goals_path, DEFAULT_GOALS_BODY),
            relative_path=str(goals_path.relative_to(repo_root)),
        )
    )
    return results


def _parse_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _parse_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _get_nested(data: Dict[str, Any], *parts: str) -> Any:
    current: Any = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _normalize_conflict_value(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(value)
    return value


def _choose_import_value(
    key: str,
    values: Dict[str, Any],
    *,
    default: Any,
    prefer: Optional[str],
    warnings: List[str],
    conflicts: List[ImportConflict],
) -> Any:
    present = {source: value for source, value in values.items() if value not in (None, "", [], ())}
    if not present:
        return default
    seen: Dict[Any, List[str]] = {}
    for source, value in present.items():
        seen.setdefault(_normalize_conflict_value(value), []).append(source)
    if len(seen) == 1:
        return next(iter(present.values()))
    if prefer and prefer in present:
        warnings.append(f"{key} differed across local files; chose {prefer}")
        return present[prefer]
    conflicts.append(ImportConflict(key=key, values=present))
    return default


def import_existing_install(repo_root: Path, *, prefer: Optional[str] = None) -> ImportResult:
    cfg = CanonicalConfig()
    warnings: List[str] = []
    conflicts: List[ImportConflict] = []
    imported_keys: List[str] = []

    env_values = _parse_dotenv(repo_root / ".env")
    codex_values = parse_toml_like((repo_root / ".codex/config.toml").read_text(encoding="utf-8")) if (repo_root / ".codex/config.toml").exists() else {}
    claude_values = _parse_json_file(repo_root / "src/config/claude_mcp.json")
    manifest_values = _parse_json_file(repo_root / DEFAULT_MANIFEST_PATH)

    token = _choose_import_value(
        "slack.user_token",
        {
            "env": env_values.get("SLACK_USER_TOKEN", ""),
            "codex": _get_nested(codex_values, "mcp_servers", "slack", "env", "SLACK_MCP_XOXP_TOKEN"),
            "claude": _get_nested(claude_values, "mcpServers", "slack", "env", "SLACK_MCP_XOXP_TOKEN"),
        },
        default=cfg.slack.user_token,
        prefer=prefer,
        warnings=warnings,
        conflicts=conflicts,
    )
    if token:
        cfg.slack.user_token = str(token)
        imported_keys.append("slack.user_token")

    manifest_name = _get_nested(manifest_values, "display_information", "name")
    manifest_description = _get_nested(manifest_values, "display_information", "description")
    if isinstance(manifest_name, str) and manifest_name.strip():
        cfg.slack.app_name = manifest_name.strip()
        imported_keys.append("slack.app_name")
    if isinstance(manifest_description, str) and manifest_description.strip():
        cfg.slack.app_description = manifest_description.strip()
        imported_keys.append("slack.app_description")

    if env_values.get("AGENT_NAME"):
        cfg.agent.name = env_values["AGENT_NAME"].strip()
        imported_keys.append("agent.name")
    elif infer_agent_name_from_app_name(cfg.slack.app_name):
        cfg.agent.name = infer_agent_name_from_app_name(cfg.slack.app_name) or cfg.agent.name

    if env_values.get("DEFAULT_CHANNEL_ID"):
        cfg.slack.default_channel_id = env_values["DEFAULT_CHANNEL_ID"].strip()
        imported_keys.append("slack.default_channel_id")
    if env_values.get("AGENT_USER_ID"):
        cfg.slack.agent_user_id = env_values["AGENT_USER_ID"].strip()
        imported_keys.append("slack.agent_user_id")

    if env_values.get("MAX_CONCURRENT_WORKERS"):
        cfg.runtime.max_concurrent_workers = int(env_values["MAX_CONCURRENT_WORKERS"])
        imported_keys.append("runtime.max_concurrent_workers")
    if env_values.get("SESSION_MINUTES"):
        cfg.runtime.session_minutes = int(env_values["SESSION_MINUTES"])
        imported_keys.append("runtime.session_minutes")

    if env_values.get("WORKER_CMD"):
        cfg.worker.command = env_values["WORKER_CMD"]
        imported_keys.append("worker.command")
    if env_values.get("DEV_REVIEW_CMD") is not None:
        cfg.developer_review.command = env_values["DEV_REVIEW_CMD"]
        cfg.developer_review.enabled = bool(cfg.developer_review.command.strip())
        imported_keys.append("developer_review.command")

    if env_values.get("TRIBUNE_CMD"):
        cfg.tribune.command = env_values["TRIBUNE_CMD"]
        imported_keys.append("tribune.command")
    if env_values.get("TRIBUNE_FALLBACK_MODELS"):
        cfg.tribune.fallback_models = [
            part.strip()
            for part in env_values["TRIBUNE_FALLBACK_MODELS"].split(",")
            if part.strip()
        ]
        imported_keys.append("tribune.fallback_models")
    if env_values.get("TRIBUNE_MAX_REVIEW_ROUNDS"):
        cfg.tribune.review_rounds = int(env_values["TRIBUNE_MAX_REVIEW_ROUNDS"])
        imported_keys.append("tribune.review_rounds")
    if env_values.get("TRIBUNE_MAINT_ROUNDS"):
        cfg.tribune.maintenance_rounds = int(env_values["TRIBUNE_MAINT_ROUNDS"])
        imported_keys.append("tribune.maintenance_rounds")
    cfg.tribune.enabled = cfg.tribune.review_rounds > 0 or cfg.tribune.maintenance_rounds > 0

    if env_values.get("DASHBOARD_EXPORT_ENABLED"):
        cfg.dashboard.export_enabled = _coerce_value(
            env_values["DASHBOARD_EXPORT_ENABLED"], cfg.dashboard.export_enabled
        )
        imported_keys.append("dashboard.export_enabled")
    if env_values.get("DASHBOARD_EXPORT_DIR"):
        cfg.dashboard.export_dir = env_values["DASHBOARD_EXPORT_DIR"]
        imported_keys.append("dashboard.export_dir")
    if env_values.get("DASHBOARD_GIT_PUSH"):
        cfg.dashboard.git_push = _coerce_value(
            env_values["DASHBOARD_GIT_PUSH"], cfg.dashboard.git_push
        )
        imported_keys.append("dashboard.git_push")
    if env_values.get("DASHBOARD_GIT_REMOTE"):
        cfg.dashboard.git_remote = env_values["DASHBOARD_GIT_REMOTE"]
        imported_keys.append("dashboard.git_remote")
    if env_values.get("DASHBOARD_GIT_BRANCH"):
        cfg.dashboard.git_branch = env_values["DASHBOARD_GIT_BRANCH"]
        imported_keys.append("dashboard.git_branch")
    if env_values.get("DASHBOARD_CF_PAGES_ENABLED"):
        cfg.dashboard.cf_pages_enabled = _coerce_value(
            env_values["DASHBOARD_CF_PAGES_ENABLED"], cfg.dashboard.cf_pages_enabled
        )
        imported_keys.append("dashboard.cf_pages_enabled")
    if env_values.get("DASHBOARD_CF_PAGES_PROJECT"):
        cfg.dashboard.cf_pages_project = env_values["DASHBOARD_CF_PAGES_PROJECT"]
        imported_keys.append("dashboard.cf_pages_project")
    if env_values.get("DASHBOARD_CF_PAGES_DIR"):
        cfg.dashboard.cf_pages_dir = env_values["DASHBOARD_CF_PAGES_DIR"]
        imported_keys.append("dashboard.cf_pages_dir")
    if env_values.get("DASHBOARD_CF_PAGES_INTERVAL"):
        cfg.dashboard.cf_pages_interval = int(env_values["DASHBOARD_CF_PAGES_INTERVAL"])
        imported_keys.append("dashboard.cf_pages_interval")
    if env_values.get("DASHBOARD_GPU_MONITOR"):
        cfg.dashboard.gpu_monitor = _coerce_value(
            env_values["DASHBOARD_GPU_MONITOR"], cfg.dashboard.gpu_monitor
        )
        imported_keys.append("dashboard.gpu_monitor")
    if env_values.get("DASHBOARD_GPU_NODE_ALIAS"):
        cfg.dashboard.gpu_node_alias = env_values["DASHBOARD_GPU_NODE_ALIAS"]
        imported_keys.append("dashboard.gpu_node_alias")
    if env_values.get("DASHBOARD_GPU_COMMAND_TIMEOUT"):
        cfg.dashboard.gpu_command_timeout = int(env_values["DASHBOARD_GPU_COMMAND_TIMEOUT"])
        imported_keys.append("dashboard.gpu_command_timeout")

    if codex_values:
        for key_name, attr_name in (
            ("model", "model"),
            ("model_reasoning_effort", "reasoning_effort"),
            ("personality", "personality"),
            ("approval_policy", "approval_policy"),
            ("sandbox_mode", "sandbox_mode"),
            ("web_search", "web_search"),
        ):
            value = codex_values.get(key_name)
            if isinstance(value, str) and value:
                setattr(cfg.worker, attr_name, value)
                imported_keys.append(f"worker.{attr_name}")

        consult_command = _get_nested(codex_values, "mcp_servers", "consult", "command")
        consult_args = _get_nested(codex_values, "mcp_servers", "consult", "args")
        consult_project = _get_nested(
            codex_values,
            "mcp_servers",
            "consult",
            "env",
            "CHATGPT_DEFAULT_PROJECT",
        )
        if isinstance(consult_command, str):
            cfg.consult.command = consult_command
            imported_keys.append("consult.command")
        if isinstance(consult_args, list):
            cfg.consult.args = [str(item) for item in consult_args]
            imported_keys.append("consult.args")
        if isinstance(consult_project, str) and consult_project:
            cfg.worker.chatgpt_project = consult_project
            imported_keys.append("worker.chatgpt_project")

    manifest_path = repo_root / DEFAULT_MANIFEST_PATH
    if manifest_path.exists():
        cfg.files.manifest_path = str(DEFAULT_MANIFEST_PATH)

    return ImportResult(
        config=cfg,
        warnings=warnings,
        conflicts=conflicts,
        imported_keys=sorted(set(imported_keys)),
    )


def format_import_conflicts(conflicts: Sequence[ImportConflict]) -> str:
    lines: List[str] = []
    for conflict in conflicts:
        lines.append(f"- {conflict.key}")
        for source, value in sorted(conflict.values.items()):
            lines.append(f"  {source}: {value}")
    return "\n".join(lines)


def _binary_from_command(command: str) -> str:
    parts = shlex.split(command)
    return parts[0] if parts else ""


def _redact_token(token: str) -> str:
    token = token.strip()
    if len(token) <= 8:
        return "***"
    return f"{token[:5]}...{token[-3:]}"


def doctor_config(cfg: CanonicalConfig, repo_root: Path) -> List[DoctorFinding]:
    findings: List[DoctorFinding] = []

    if not cfg.slack.user_token.strip():
        findings.append(
            DoctorFinding("error", "missing_slack_token", "Slack user token is not set.")
        )

    if shutil.which("tmux") is None:
        findings.append(DoctorFinding("error", "missing_tmux", "tmux is not installed or not on PATH."))

    worker_binary = _binary_from_command(cfg.worker.command)
    if worker_binary and shutil.which(worker_binary) is None:
        findings.append(
            DoctorFinding(
                "error",
                "missing_worker_binary",
                f"Worker binary '{worker_binary}' is not installed or not on PATH.",
            )
        )

    if cfg.developer_review.enabled:
        review_binary = _binary_from_command(cfg.developer_review.command)
        if review_binary and shutil.which(review_binary) is None:
            findings.append(
                DoctorFinding(
                    "error",
                    "missing_dev_review_binary",
                    f"Developer-review binary '{review_binary}' is not installed or not on PATH.",
                )
            )

    if cfg.tribune.enabled:
        tribune_binary = _binary_from_command(cfg.tribune.command)
        if tribune_binary and shutil.which(tribune_binary) is None:
            findings.append(
                DoctorFinding(
                    "error",
                    "missing_tribune_binary",
                    f"Tribune binary '{tribune_binary}' is not installed or not on PATH.",
                )
            )

    slack_mcp_path = Path(_slack_mcp_command(repo_root))
    if not slack_mcp_path.exists():
        findings.append(
            DoctorFinding(
                "warning",
                "missing_slack_mcp_binary",
                f"Slack MCP binary is missing at {slack_mcp_path}. Build it before starting Murphy.",
            )
        )

    for path, expected in build_projection_map(cfg, repo_root).items():
        relative = str(path.relative_to(repo_root))
        if not path.exists():
            findings.append(
                DoctorFinding(
                    "error",
                    "missing_projection",
                    f"Generated projection is missing: {relative}",
                )
            )
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            findings.append(
                DoctorFinding(
                    "warning",
                    "projection_out_of_sync",
                    f"Generated projection differs from canonical config (likely a hand edit): {relative}. Run `murphy config sync --force` to overwrite.",
                )
            )

    return findings


def effective_runtime_view(cfg: CanonicalConfig, repo_root: Path) -> Dict[str, Any]:
    from src.loop.supervisor.config import Config
    from src.loop.supervisor.main import DEFAULT_LOOP_CONFIG_FILE

    env_values = _parse_dotenv_from_text(render_env(cfg))
    runtime = Config(repo_root / DEFAULT_LOOP_CONFIG_FILE, env=env_values)

    return {
        "agent_name": runtime.agent_name,
        "agent_user_id": getattr(runtime, "agent_user_id", ""),
        "default_channel_id": runtime.default_channel_id,
        "session_minutes": runtime.session_minutes,
        "max_concurrent_workers": runtime.max_concurrent_workers,
        "worker_cmd": runtime.worker_cmd,
        "dev_review_cmd": runtime.dev_review_cmd,
        "tribune_cmd": runtime.tribune_cmd,
        "tribune_fallback_models": runtime.tribune_fallback_models,
        "tribune_max_review_rounds": runtime.tribune_max_review_rounds,
        "tribune_maint_rounds": runtime.tribune_maint_rounds,
        "dashboard_export_enabled": env_values.get("DASHBOARD_EXPORT_ENABLED", "false"),
        "dashboard_export_dir": env_values.get("DASHBOARD_EXPORT_DIR", ""),
        "slack_user_token": _redact_token(cfg.slack.user_token),
    }


def _parse_dotenv_from_text(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def format_projection_results(results: Iterable[ProjectionResult]) -> str:
    grouped: Dict[str, List[str]] = {}
    for result in results:
        grouped.setdefault(result.status, []).append(result.relative_path)
    lines: List[str] = []
    for status in ("created", "updated", "unchanged", "skipped"):
        paths = grouped.get(status, [])
        if not paths:
            continue
        lines.append(f"{status.title()}:")
        for rel_path in paths:
            lines.append(f"  - {rel_path}")
    return "\n".join(lines)


def format_doctor_findings(findings: Iterable[DoctorFinding]) -> str:
    lines: List[str] = []
    for finding in findings:
        lines.append(f"[{finding.level}] {finding.message}")
    return "\n".join(lines)
