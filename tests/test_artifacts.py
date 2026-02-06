from pathlib import Path

from codex_telegram.artifacts import ArtifactService
from codex_telegram.config import Settings
from codex_telegram.db import Database
from codex_telegram.models import JobMode, RiskLevel
from codex_telegram.repository import Repository


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="token",
        owner_telegram_id=1,
        sqlite_path=tmp_path / "state.sqlite3",
        runs_dir=tmp_path / "runs",
        codex_workdir=tmp_path,
        codex_allowed_workdirs=(tmp_path,),
        codex_ephemeral_cmd_template="echo",
        codex_session_cmd_template="echo",
        codex_session_boot_cmd_template=None,
        codex_skip_git_repo_check=True,
        worker_poll_interval=0.1,
        max_parallel_jobs=1,
        job_timeout_seconds=60,
        command_cooldown_seconds=0.0,
        max_artifact_bytes=1_000_000,
        allowed_artifact_extensions=(".log", ".txt", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".pdf"),
        log_level="INFO",
    )


def test_register_file_skips_empty_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    svc = ArtifactService(repo, settings)
    job = repo.create_job(
        prompt="test",
        mode=JobMode.EPHEMERAL,
        session_name=None,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
    )

    empty_file = tmp_path / "empty.log"
    empty_file.write_text("", encoding="utf-8")

    artifact = svc.register_file(job_id=job.id, path=empty_file)

    assert artifact is None


def test_register_file_keeps_non_empty_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    svc = ArtifactService(repo, settings)
    job = repo.create_job(
        prompt="test",
        mode=JobMode.EPHEMERAL,
        session_name=None,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
    )

    file_path = tmp_path / "stdout.log"
    file_path.write_text("hello\n", encoding="utf-8")

    artifact = svc.register_file(job_id=job.id, path=file_path)

    assert artifact is not None
    assert artifact.size_bytes > 0
