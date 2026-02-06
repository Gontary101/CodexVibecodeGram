from datetime import UTC, datetime
from pathlib import Path
import shlex

import pytest

from codex_telegram.config import Settings
from codex_telegram.executor import CodexExecutor, RuntimeProfileError
from codex_telegram.models import ExecutionContext, Job, JobMode, JobStatus, RiskLevel


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
        codex_session_cmd_template="codex exec --skip-git-repo-check resume {session_name_quoted} {prompt_quoted}",
        codex_session_boot_cmd_template=None,
        codex_skip_git_repo_check=True,
        codex_auto_safe_flags=True,
        codex_safe_default_approval="on-request",
        worker_poll_interval=0.1,
        max_parallel_jobs=1,
        job_timeout_seconds=60,
        command_cooldown_seconds=0.0,
        max_artifact_bytes=1_000_000,
        allowed_artifact_extensions=(".log", ".txt", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".pdf"),
        telegram_response_mode="natural",
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


def _count_output_last_message_flags(command: str) -> int:
    tokens = shlex.split(command)
    count = 0
    for token in tokens:
        if token in {"-o", "--output-last-message"}:
            count += 1
        elif token.startswith("-o=") or token.startswith("--output-last-message="):
            count += 1
    return count


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


def test_set_workdir_allows_only_allowed_roots(tmp_path: Path) -> None:
    allowed_root = tmp_path / "workspace"
    target = allowed_root / "project"
    allowed_root.mkdir(parents=True)
    target.mkdir()

    settings = _settings(tmp_path)
    settings.codex_workdir = allowed_root
    settings.codex_allowed_workdirs = (allowed_root,)
    executor = CodexExecutor(settings)

    executor.set_workdir("project")
    assert executor.get_effective_workdir() == target.resolve()

    with pytest.raises(RuntimeProfileError):
        executor.set_workdir("/tmp")


def test_set_workdir_reset_restores_default(tmp_path: Path) -> None:
    allowed_root = tmp_path / "workspace"
    nested = allowed_root / "nested"
    allowed_root.mkdir(parents=True)
    nested.mkdir()

    settings = _settings(tmp_path)
    settings.codex_workdir = allowed_root
    settings.codex_allowed_workdirs = (allowed_root,)
    executor = CodexExecutor(settings)

    executor.set_workdir("nested")
    assert executor.get_effective_workdir() == nested.resolve()

    executor.set_workdir(None)
    assert executor.get_effective_workdir() == allowed_root.resolve()


def test_default_approval_and_output_path_are_injected(tmp_path: Path) -> None:
    executor = CodexExecutor(_settings(tmp_path))

    output_path = tmp_path / "run" / "assistant_last_message.txt"
    plan = executor.build_plan(_ctx(tmp_path, "hello"), output_last_message_path=output_path)

    assert "approval_policy=\"on-request\"" in plan.command
    assert "--output-last-message" in plan.command or " -o " in plan.command
    assert str(output_path) in plan.command
    assert _count_output_last_message_flags(plan.command) == 1


def test_output_path_injection_ignores_o_in_prompt_text(tmp_path: Path) -> None:
    executor = CodexExecutor(_settings(tmp_path))

    output_path = tmp_path / "run" / "assistant_last_message.txt"
    plan = executor.build_plan(
        _ctx(tmp_path, "Please explain why the text contains -o in the middle"),
        output_last_message_path=output_path,
    )

    assert "approval_policy=\"on-request\"" in plan.command
    assert "--output-last-message" in plan.command or " -o " in plan.command
    assert str(output_path) in plan.command
    assert _count_output_last_message_flags(plan.command) == 1


def test_output_path_injection_does_not_duplicate_existing_flag_after_positionals(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.codex_session_cmd_template = (
        "codex exec resume {session_name_quoted} {prompt_quoted} "
        "--output-last-message {output_last_message_path_quoted}"
    )
    executor = CodexExecutor(settings)

    now = datetime.now(UTC)
    job = Job(
        id=7,
        status=JobStatus.RUNNING,
        mode=JobMode.SESSION,
        prompt="hello from session",
        created_at=now,
        updated_at=now,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
        session_name="feature-branch",
    )
    ctx = ExecutionContext(job=job, run_dir=tmp_path / "run", approved=True)
    output_path = tmp_path / "run" / "assistant_last_message.txt"

    plan = executor.build_plan(ctx, output_last_message_path=output_path)

    assert str(output_path) in plan.command
    assert _count_output_last_message_flags(plan.command) == 1


def test_auto_safe_flags_can_disable_skip_git_repo_check(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.codex_auto_safe_flags = False
    executor = CodexExecutor(settings)

    plan = executor.build_plan(_ctx(tmp_path, "hello"))

    assert "--skip-git-repo-check" not in plan.command
