import asyncio
from pathlib import Path

import pytest

from codex_telegram.artifacts import ArtifactService
from codex_telegram.config import Settings
from codex_telegram.db import Database
from codex_telegram.models import ExecutionContext, ExecutionResult, JobMode, JobStatus
from codex_telegram.orchestrator import Orchestrator
from codex_telegram.policy import RiskPolicy
from codex_telegram.repository import Repository
from codex_telegram.sessions import SessionManager


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, text: str) -> None:
        self.messages.append(text)

    async def send_job_status(self, job, heading: str) -> None:  # type: ignore[no-untyped-def]
        self.messages.append(f"{heading}:{job.id}:{job.status}")

    async def send_artifacts(self, artifacts) -> None:  # type: ignore[no-untyped-def]
        self.messages.append(f"artifacts:{len(artifacts)}")


class FakeExecutor:
    async def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        stdout = ctx.run_dir / "stdout.log"
        stderr = ctx.run_dir / "stderr.log"
        stdout.write_text("done", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return ExecutionResult(
            exit_code=0,
            stdout_path=stdout,
            stderr_path=stderr,
            summary="ok",
            error_text=None,
        )


@pytest.mark.asyncio
async def test_orchestrator_runs_low_risk_job(tmp_path: Path) -> None:
    settings = Settings(
        telegram_bot_token="token",
        owner_telegram_id=1,
        sqlite_path=tmp_path / "state.sqlite3",
        runs_dir=tmp_path / "runs",
        codex_workdir=tmp_path,
        codex_ephemeral_cmd_template="echo",
        codex_session_cmd_template="echo",
        codex_session_boot_cmd_template=None,
        codex_skip_git_repo_check=True,
        worker_poll_interval=0.05,
        max_parallel_jobs=1,
        job_timeout_seconds=10,
        command_cooldown_seconds=0.0,
        max_artifact_bytes=5_000_000,
        allowed_artifact_extensions=(".log", ".txt", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".pdf"),
        log_level="INFO",
    )
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    policy = RiskPolicy()
    notifier = FakeNotifier()
    artifacts = ArtifactService(repo, settings)
    session_manager = SessionManager(repo, settings)

    orchestrator = Orchestrator(
        repo=repo,
        policy=policy,
        executor=FakeExecutor(),  # type: ignore[arg-type]
        artifact_service=artifacts,
        session_manager=session_manager,
        settings=settings,
        notifier=notifier,  # type: ignore[arg-type]
    )

    await orchestrator.start()
    job = await orchestrator.submit_job("echo hello", JobMode.EPHEMERAL)

    for _ in range(40):
        current = repo.get_job(job.id)
        if current.status == JobStatus.SUCCEEDED:
            break
        await asyncio.sleep(0.05)

    current = repo.get_job(job.id)
    assert current.status == JobStatus.SUCCEEDED
    await orchestrator.stop()


@pytest.mark.asyncio
async def test_orchestrator_waits_for_approval(tmp_path: Path) -> None:
    settings = Settings(
        telegram_bot_token="token",
        owner_telegram_id=1,
        sqlite_path=tmp_path / "state.sqlite3",
        runs_dir=tmp_path / "runs",
        codex_workdir=tmp_path,
        codex_ephemeral_cmd_template="echo",
        codex_session_cmd_template="echo",
        codex_session_boot_cmd_template=None,
        codex_skip_git_repo_check=True,
        worker_poll_interval=0.05,
        max_parallel_jobs=1,
        job_timeout_seconds=10,
        command_cooldown_seconds=0.0,
        max_artifact_bytes=5_000_000,
        allowed_artifact_extensions=(".log", ".txt", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".pdf"),
        log_level="INFO",
    )
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    policy = RiskPolicy()
    notifier = FakeNotifier()
    artifacts = ArtifactService(repo, settings)
    session_manager = SessionManager(repo, settings)

    orchestrator = Orchestrator(
        repo=repo,
        policy=policy,
        executor=FakeExecutor(),  # type: ignore[arg-type]
        artifact_service=artifacts,
        session_manager=session_manager,
        settings=settings,
        notifier=notifier,  # type: ignore[arg-type]
    )

    await orchestrator.start()
    job = await orchestrator.submit_job("sudo rm -rf /tmp/test", JobMode.EPHEMERAL)
    current = repo.get_job(job.id)
    assert current.status == JobStatus.AWAITING_APPROVAL

    await orchestrator.approve_job(job.id, user_id=1)
    for _ in range(40):
        current = repo.get_job(job.id)
        if current.status == JobStatus.SUCCEEDED:
            break
        await asyncio.sleep(0.05)

    current = repo.get_job(job.id)
    assert current.status == JobStatus.SUCCEEDED
    await orchestrator.stop()
