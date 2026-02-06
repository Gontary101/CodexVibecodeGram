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
        telegram_business_connection_id=None,
        sqlite_path=tmp_path / "state.sqlite3",
        runs_dir=tmp_path / "runs",
        codex_workdir=tmp_path,
        codex_allowed_workdirs=(tmp_path,),
        codex_ephemeral_cmd_template="echo",
        codex_session_cmd_template="echo",
        codex_session_boot_cmd_template=None,
        codex_skip_git_repo_check=True,
        codex_auto_safe_flags=True,
        codex_safe_default_approval="on-request",
        worker_poll_interval=0.1,
        max_parallel_jobs=1,
        job_timeout_seconds=60,
        command_cooldown_seconds=0.0,
        max_artifact_bytes=1_000_000,
        allowed_artifact_extensions=(
            ".log",
            ".txt",
            ".json",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".mp4",
            ".pdf",
        ),
        telegram_response_mode="natural",
        log_level="INFO",
    )


def test_collect_from_output_texts_registers_referenced_image(tmp_path: Path) -> None:
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

    img = tmp_path / "runs" / "snake_screenshots" / "snake_01_start.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"pngbytes")

    added = svc.collect_from_output_texts(
        job.id,
        ["Generated: `runs/snake_screenshots/snake_01_start.png`"],
        base_dir=tmp_path,
        roots=[tmp_path],
    )

    assert len(added) == 1
    assert added[0].kind == "image"
    assert added[0].path == img.resolve()


def test_collect_from_output_texts_ignores_outside_root(tmp_path: Path) -> None:
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

    outside = Path("/tmp/outside_demo.png")
    outside.write_bytes(b"x")
    try:
        added = svc.collect_from_output_texts(
            job.id,
            [f"Output: `{outside}`"],
            base_dir=tmp_path,
            roots=[tmp_path],
        )
    finally:
        outside.unlink(missing_ok=True)

    assert added == []
