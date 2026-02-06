from __future__ import annotations

from pathlib import Path

import pytest

from codex_telegram.config import Settings
from codex_telegram.db import Database
from codex_telegram.models import SessionStatus
from codex_telegram.repository import Repository
from codex_telegram.sessions import SessionManager


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="token",
        owner_telegram_id=1,
        telegram_business_connection_id=None,
        sqlite_path=tmp_path / "state.sqlite3",
        runs_dir=tmp_path / "runs",
        codex_workdir=tmp_path,
        codex_allowed_workdirs=(tmp_path,),
        codex_ephemeral_cmd_template="codex exec {prompt_quoted}",
        codex_session_cmd_template="codex exec resume {session_name_quoted} {prompt_quoted}",
        codex_session_boot_cmd_template=None,
        codex_skip_git_repo_check=True,
        codex_auto_safe_flags=True,
        codex_safe_default_approval="on-request",
        worker_poll_interval=0.05,
        max_parallel_jobs=1,
        job_timeout_seconds=10,
        command_cooldown_seconds=0.0,
        max_artifact_bytes=5_000_000,
        allowed_artifact_extensions=(".log", ".txt", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".pdf"),
        telegram_response_mode="natural",
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_session_manager_create_stop_and_list(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    manager = SessionManager(repo=repo, settings=settings)

    created = await manager.create("alpha")
    assert created.created is True
    assert created.record.name == "alpha"
    assert created.record.status == SessionStatus.ACTIVE
    assert manager.is_session_active("alpha") is True

    listed = manager.list_sessions()
    assert [s.name for s in listed] == ["alpha"]

    stopped = await manager.stop("alpha")
    assert stopped.status == SessionStatus.INACTIVE
    assert manager.is_session_active("alpha") is False


@pytest.mark.asyncio
async def test_session_manager_create_existing_active_is_idempotent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    manager = SessionManager(repo=repo, settings=settings)

    first = await manager.create("beta")
    second = await manager.create("beta")

    assert first.created is True
    assert second.created is False
    assert second.record.status == SessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_session_manager_stop_unknown_raises(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    manager = SessionManager(repo=repo, settings=settings)

    with pytest.raises(KeyError):
        await manager.stop("does-not-exist")
