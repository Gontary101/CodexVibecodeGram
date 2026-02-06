from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


DEFAULT_ALLOWED_EXTENSIONS = (
    ".txt",
    ".log",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".pdf",
)
ALLOWED_RESPONSE_MODES = {"natural", "compact", "verbose"}
ALLOWED_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    owner_telegram_id: int
    sqlite_path: Path
    runs_dir: Path
    codex_workdir: Path
    codex_allowed_workdirs: tuple[Path, ...]
    codex_ephemeral_cmd_template: str
    codex_session_cmd_template: str
    codex_session_boot_cmd_template: str | None
    codex_skip_git_repo_check: bool
    codex_auto_safe_flags: bool
    codex_safe_default_approval: str
    worker_poll_interval: float
    max_parallel_jobs: int
    job_timeout_seconds: int
    command_cooldown_seconds: float
    max_artifact_bytes: int
    allowed_artifact_extensions: tuple[str, ...]
    telegram_response_mode: str
    log_level: str


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _require_str(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value is not None else default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value is not None else default


def _get_path(name: str, default: str) -> Path:
    value = os.getenv(name, default)
    return Path(value).expanduser().resolve()


def _get_path_list(name: str) -> tuple[Path, ...]:
    raw = os.getenv(name, "")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return tuple(Path(item).expanduser().resolve() for item in values)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid boolean value for {name}: {value}")


def _get_extensions() -> tuple[str, ...]:
    value = os.getenv("ALLOWED_ARTIFACT_EXTENSIONS")
    if not value:
        return DEFAULT_ALLOWED_EXTENSIONS
    parsed = tuple(x.strip().lower() for x in value.split(",") if x.strip())
    return parsed or DEFAULT_ALLOWED_EXTENSIONS


def _get_choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ConfigError(f"Invalid value for {name}: {value}. Allowed: {allowed_values}")
    return value


def load_settings() -> Settings:
    _load_env_file(Path(".env"))

    codex_workdir = _get_path("CODEX_WORKDIR", ".")
    allowed_workdirs = _get_path_list("CODEX_ALLOWED_WORKDIRS")
    if not allowed_workdirs:
        allowed_workdirs = (codex_workdir,)
    for root in allowed_workdirs:
        if root == codex_workdir and not root.exists():
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists() or not root.is_dir():
            raise ConfigError(f"Invalid CODEX_ALLOWED_WORKDIRS entry (not a directory): {root}")
    if not any(_is_within(codex_workdir, root) for root in allowed_workdirs):
        raise ConfigError("CODEX_WORKDIR must be inside CODEX_ALLOWED_WORKDIRS")

    settings = Settings(
        telegram_bot_token=_require_str("TELEGRAM_BOT_TOKEN"),
        owner_telegram_id=int(_require_str("OWNER_TELEGRAM_ID")),
        sqlite_path=_get_path("SQLITE_PATH", "data/state.sqlite3"),
        runs_dir=_get_path("RUNS_DIR", "runs"),
        codex_workdir=codex_workdir,
        codex_allowed_workdirs=allowed_workdirs,
        codex_ephemeral_cmd_template=os.getenv(
            "CODEX_EPHEMERAL_CMD_TEMPLATE",
            "codex exec {prompt_quoted}",
        ),
        codex_session_cmd_template=os.getenv(
            "CODEX_SESSION_CMD_TEMPLATE",
            "codex exec --skip-git-repo-check resume {session_name_quoted} {prompt_quoted}",
        ),
        codex_session_boot_cmd_template=os.getenv("CODEX_SESSION_BOOT_CMD_TEMPLATE"),
        codex_skip_git_repo_check=_get_bool("CODEX_SKIP_GIT_REPO_CHECK", True),
        codex_auto_safe_flags=_get_bool("CODEX_AUTO_SAFE_FLAGS", True),
        codex_safe_default_approval=_get_choice(
            "CODEX_SAFE_DEFAULT_APPROVAL",
            "on-request",
            ALLOWED_APPROVAL_POLICIES,
        ),
        worker_poll_interval=_get_float("WORKER_POLL_INTERVAL", 0.5),
        max_parallel_jobs=_get_int("MAX_PARALLEL_JOBS", 1),
        job_timeout_seconds=_get_int("JOB_TIMEOUT_SECONDS", 3600),
        command_cooldown_seconds=_get_float("COMMAND_COOLDOWN_SECONDS", 1.0),
        max_artifact_bytes=_get_int("MAX_ARTIFACT_BYTES", 50_000_000),
        allowed_artifact_extensions=_get_extensions(),
        telegram_response_mode=_get_choice("TELEGRAM_RESPONSE_MODE", "natural", ALLOWED_RESPONSE_MODES),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )

    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.codex_workdir.mkdir(parents=True, exist_ok=True)
    return settings
