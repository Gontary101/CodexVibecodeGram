from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_telegram.config import Settings
from codex_telegram.executor import CodexExecutor, RuntimeProfileError
from codex_telegram.models import ExecutionContext, Job, JobMode, JobStatus, RiskLevel


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="token",
        owner_telegram_id=1,
        sqlite_path=tmp_path / "state.sqlite3",
        runs_dir=tmp_path / "runs",
        codex_workdir=tmp_path,
        codex_ephemeral_cmd_template="codex exec {prompt_quoted}",
        codex_session_cmd_template="codex exec --skip-git-repo-check resume {session_name_quoted} {prompt_quoted}",
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


def _ctx(tmp_path: Path, prompt: str = "hello") -> ExecutionContext:
    now = datetime.now(UTC)
    job = Job(
        id=1,
        status=JobStatus.RUNNING,
        mode=JobMode.EPHEMERAL,
        prompt=prompt,
        created_at=now,
        updated_at=now,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
    )
    return ExecutionContext(job=job, run_dir=tmp_path / "run", approved=True)


def test_runtime_flags_are_injected(tmp_path: Path) -> None:
    executor = CodexExecutor(_settings(tmp_path))

    executor.set_model("gpt-5-codex", "high")
    executor.set_sandbox_mode("workspace-write")
    executor.set_approval_policy("on-request")
    executor.set_web_search_mode("cached")
    executor.set_experimental_feature("new-tool", enabled=True)

    plan = executor.build_plan(_ctx(tmp_path, "say hi"))

    assert plan.command.startswith("codex exec ")
    assert "--skip-git-repo-check" in plan.command
    assert "-m gpt-5-codex" in plan.command
    assert "model_reasoning_effort=\"high\"" in plan.command
    assert "-s workspace-write" in plan.command
    assert "approval_policy=\"on-request\"" in plan.command
    assert "web_search=\"cached\"" in plan.command
    assert "--enable new-tool" in plan.command


def test_personality_prefix_is_applied(tmp_path: Path) -> None:
    executor = CodexExecutor(_settings(tmp_path))
    executor.set_personality("concise")

    plan = executor.build_plan(_ctx(tmp_path, "Explain this"))

    assert "Respond concisely" in plan.command
    assert "Explain this" in plan.command


def test_invalid_runtime_values_raise(tmp_path: Path) -> None:
    executor = CodexExecutor(_settings(tmp_path))

    with pytest.raises(RuntimeProfileError):
        executor.set_sandbox_mode("unsafe")
    with pytest.raises(RuntimeProfileError):
        executor.set_approval_policy("always")
    with pytest.raises(RuntimeProfileError):
        executor.set_model("gpt-5-codex", "extreme")
    with pytest.raises(RuntimeProfileError):
        executor.set_web_search_mode("always")
