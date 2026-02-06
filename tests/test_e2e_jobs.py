from __future__ import annotations

import asyncio
import os
import textwrap
import time
from pathlib import Path

import pytest

from codex_telegram.artifacts import ArtifactService
from codex_telegram.config import Settings
from codex_telegram.db import Database
from codex_telegram.executor import CodexExecutor
from codex_telegram.models import JobMode, JobStatus
from codex_telegram.orchestrator import Orchestrator
from codex_telegram.policy import RiskPolicy
from codex_telegram.repository import Repository
from codex_telegram.sessions import SessionManager


class RecordingNotifier:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.statuses: list[tuple[int, JobStatus, str]] = []
        self.artifact_batches: list[int] = []

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def send_job_status(self, job, heading: str) -> None:  # type: ignore[no-untyped-def]
        self.statuses.append((job.id, job.status, heading))

    async def send_artifacts(self, artifacts) -> None:  # type: ignore[no-untyped-def]
        self.artifact_batches.append(len(artifacts))


def _install_fake_codex(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    codex_path = bin_dir / "codex"
    codex_path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import pathlib
            import sys

            def parse_exec(argv):
                output_path = None
                positionals = []
                i = 0
                takes_value = {
                    "-o", "--output-last-message",
                    "-m", "-s", "-C", "--cd", "-p", "--profile",
                    "-c", "--config", "--enable", "--disable",
                    "--add-dir", "-i", "--image", "--output-schema",
                    "--color", "--local-provider",
                }
                flags_only = {
                    "--skip-git-repo-check", "--json", "--full-auto",
                    "--dangerously-bypass-approvals-and-sandbox", "--oss",
                }
                while i < len(argv):
                    arg = argv[i]
                    if arg in takes_value:
                        if i + 1 < len(argv):
                            if arg in {"-o", "--output-last-message"}:
                                output_path = argv[i + 1]
                            i += 2
                            continue
                        i += 1
                        continue
                    if arg in flags_only:
                        i += 1
                        continue
                    if arg.startswith("-"):
                        i += 1
                        continue
                    positionals.append(arg)
                    i += 1
                return output_path, positionals

            def run_exec(argv):
                if any(a in {"-h", "--help"} for a in argv):
                    print("Run Codex non-interactively\\n--model <MODEL>")
                    return 0
                output_path, positionals = parse_exec(argv)
                prompt = ""
                if positionals:
                    if positionals[0] == "resume":
                        prompt = positionals[2] if len(positionals) > 2 else ""
                    elif positionals[0] == "review":
                        prompt = "review"
                    else:
                        prompt = positionals[-1]
                if "FAIL" in prompt:
                    sys.stderr.write("simulated failure for prompt\\n")
                    return 2

                response = f"Assistant response: {prompt}".strip()
                if output_path:
                    pathlib.Path(output_path).write_text(response, encoding="utf-8")

                if "MAKE_ARTIFACT" in prompt:
                    artifact_path = pathlib.Path(os.getcwd()) / "generated" / "result.png"
                    artifact_path.parent.mkdir(parents=True, exist_ok=True)
                    artifact_path.write_bytes(b"PNGDATA")
                    print(f"artifact: `{artifact_path}`")

                print(response)
                return 0

            def main():
                args = sys.argv[1:]
                if not args:
                    return 0
                if args[0] == "--version":
                    print("codex-cli 0.98.0")
                    return 0
                if args[:2] == ["features", "list"]:
                    print("collab experimental true")
                    return 0
                if args[0] == "exec":
                    return run_exec(args[1:])
                return 0

            if __name__ == "__main__":
                raise SystemExit(main())
            """
        ),
        encoding="utf-8",
    )
    codex_path.chmod(0o755)
    return bin_dir


def _settings(tmp_path: Path, workdir: Path) -> Settings:
    return Settings(
        telegram_bot_token="token",
        owner_telegram_id=1,
        sqlite_path=tmp_path / "state.sqlite3",
        runs_dir=tmp_path / "runs",
        codex_workdir=workdir,
        codex_allowed_workdirs=(workdir,),
        codex_ephemeral_cmd_template="codex exec {prompt_quoted}",
        codex_session_cmd_template="codex exec resume {session_name_quoted} {prompt_quoted}",
        codex_session_boot_cmd_template=None,
        codex_skip_git_repo_check=True,
        codex_auto_safe_flags=True,
        codex_safe_default_approval="on-request",
        worker_poll_interval=0.02,
        max_parallel_jobs=1,
        job_timeout_seconds=20,
        command_cooldown_seconds=0.0,
        max_artifact_bytes=5_000_000,
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


def _build_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Orchestrator, Repository, SessionManager, RecordingNotifier]:
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)

    bin_dir = _install_fake_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    settings = _settings(tmp_path, workdir)
    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    repo.ensure_owner(settings.owner_telegram_id)

    notifier = RecordingNotifier()
    policy = RiskPolicy()
    executor = CodexExecutor(settings)
    artifacts = ArtifactService(repo=repo, settings=settings)
    sessions = SessionManager(repo=repo, settings=settings)

    orchestrator = Orchestrator(
        repo=repo,
        policy=policy,
        executor=executor,
        artifact_service=artifacts,
        session_manager=sessions,
        settings=settings,
        notifier=notifier,  # type: ignore[arg-type]
    )
    return orchestrator, repo, sessions, notifier


async def _wait_terminal(repo: Repository, job_id: int, timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = repo.get_job(job_id)
        if job.is_terminal:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach terminal state in {timeout_s}s")


@pytest.mark.asyncio
async def test_e2e_ephemeral_success_uses_last_message_and_collects_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, repo, _, notifier = _build_stack(tmp_path, monkeypatch)
    await orchestrator.start()
    try:
        job = await orchestrator.submit_job("MAKE_ARTIFACT hello world", JobMode.EPHEMERAL)
        final = await _wait_terminal(repo, job.id)
    finally:
        await orchestrator.stop()

    assert final.status == JobStatus.SUCCEEDED
    assert "Assistant response:" in (final.summary_text or "")
    artifacts = repo.list_artifacts(job.id)
    assert any(a.kind == "image" for a in artifacts)
    assert notifier.artifact_batches
    assert max(notifier.artifact_batches) >= 1


@pytest.mark.asyncio
async def test_e2e_session_job_fails_when_session_is_inactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, repo, _, _ = _build_stack(tmp_path, monkeypatch)
    await orchestrator.start()
    try:
        job = await orchestrator.submit_job(
            "hello from inactive session",
            JobMode.SESSION,
            session_name="missing-session",
        )
        final = await _wait_terminal(repo, job.id)
    finally:
        await orchestrator.stop()

    assert final.status == JobStatus.FAILED
    assert "inactive" in (final.error_text or "").lower()


@pytest.mark.asyncio
async def test_e2e_session_job_succeeds_when_session_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, repo, sessions, _ = _build_stack(tmp_path, monkeypatch)
    await sessions.create("demo-session")
    await orchestrator.start()
    try:
        job = await orchestrator.submit_job(
            "hello from active session",
            JobMode.SESSION,
            session_name="demo-session",
        )
        final = await _wait_terminal(repo, job.id)
    finally:
        await orchestrator.stop()

    assert final.status == JobStatus.SUCCEEDED
    assert "active session" in (final.summary_text or "")


@pytest.mark.asyncio
async def test_e2e_approval_flow_blocks_until_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, repo, _, notifier = _build_stack(tmp_path, monkeypatch)
    await orchestrator.start()
    try:
        job = await orchestrator.submit_job("sudo echo hello", JobMode.EPHEMERAL)
        awaiting = repo.get_job(job.id)
        assert awaiting.status == JobStatus.AWAITING_APPROVAL
        assert any("waiting for approval" in msg.lower() for msg in notifier.texts)

        await orchestrator.approve_job(job.id, user_id=1)
        final = await _wait_terminal(repo, job.id)
    finally:
        await orchestrator.stop()

    assert final.status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_e2e_failure_path_surfaces_executor_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, repo, _, notifier = _build_stack(tmp_path, monkeypatch)
    await orchestrator.start()
    try:
        job = await orchestrator.submit_job("FAIL please", JobMode.EPHEMERAL)
        final = await _wait_terminal(repo, job.id)
    finally:
        await orchestrator.stop()

    assert final.status == JobStatus.FAILED
    assert "simulated failure" in (final.error_text or "")
    assert any(status == JobStatus.FAILED for _, status, _ in notifier.statuses)
