from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import Settings
from .models import ExecutionContext, ExecutionPlan, ExecutionResult, JobMode

ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
ALLOWED_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
ALLOWED_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}
ALLOWED_WEB_SEARCH_MODES = {"live", "cached", "disabled"}
DEFAULT_PERSONALITY_PRESET = "none"
PERSONALITY_PRESETS = {
    "none": "",
    "friendly": "Respond in a friendly, collaborative tone.",
    "pragmatic": "Respond as a pragmatic software engineer: direct, concise, and actionable.",
    # Legacy aliases retained for backwards compatibility with prior bot releases.
    "default": "",
    "concise": "Respond concisely with the direct answer first.",
    "detailed": "Respond with structured detail and include key tradeoffs.",
    "coding": "Prioritize actionable engineering output with explicit assumptions.",
}


class RuntimeProfileError(ValueError):
    pass


@dataclass(slots=True)
class RuntimeProfile:
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox_mode: str | None = None
    approval_policy: str | None = None
    web_search: str | None = None
    experimental_features: set[str] = field(default_factory=set)
    personality: str = DEFAULT_PERSONALITY_PRESET
    personality_instruction: str = ""
    workdir_override: Path | None = None


def _normalize_feature(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _tail_text(path: Path, max_chars: int = 3200) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text.strip()
    return text[-max_chars:].strip()


def _read_text(path: Path, max_chars: int = 12000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars].strip()


class CodexExecutor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._runtime = RuntimeProfile()

    def get_runtime_profile(self) -> RuntimeProfile:
        return replace(self._runtime, experimental_features=set(self._runtime.experimental_features))

    def get_effective_approval_policy(self) -> str:
        return self._runtime.approval_policy or self._settings.codex_safe_default_approval

    def get_allowed_workdirs(self) -> tuple[Path, ...]:
        return self._settings.codex_allowed_workdirs

    def get_effective_workdir(self) -> Path:
        return self._runtime.workdir_override or self._settings.codex_workdir

    def set_model(self, model: str | None, reasoning_effort: str | None = None) -> RuntimeProfile:
        self._runtime.model = model.strip() if model else None
        if reasoning_effort is not None:
            normalized = reasoning_effort.strip().lower()
            if normalized and normalized not in ALLOWED_REASONING_EFFORTS:
                raise RuntimeProfileError(
                    f"Invalid reasoning effort '{reasoning_effort}'. Allowed: {', '.join(sorted(ALLOWED_REASONING_EFFORTS))}"
                )
            self._runtime.reasoning_effort = normalized or None
        return self.get_runtime_profile()

    def set_sandbox_mode(self, sandbox_mode: str | None) -> RuntimeProfile:
        if sandbox_mode is None:
            self._runtime.sandbox_mode = None
            return self.get_runtime_profile()
        normalized = sandbox_mode.strip().lower()
        if normalized not in ALLOWED_SANDBOX_MODES:
            raise RuntimeProfileError(
                f"Invalid permissions mode '{sandbox_mode}'. Allowed: {', '.join(sorted(ALLOWED_SANDBOX_MODES))}"
            )
        self._runtime.sandbox_mode = normalized
        return self.get_runtime_profile()

    def set_approval_policy(self, policy: str | None) -> RuntimeProfile:
        if policy is None:
            self._runtime.approval_policy = None
            return self.get_runtime_profile()
        normalized = policy.strip().lower()
        if normalized not in ALLOWED_APPROVAL_POLICIES:
            raise RuntimeProfileError(
                f"Invalid approvals policy '{policy}'. Allowed: {', '.join(sorted(ALLOWED_APPROVAL_POLICIES))}"
            )
        self._runtime.approval_policy = normalized
        return self.get_runtime_profile()

    def set_search(self, enabled: bool) -> RuntimeProfile:
        self._runtime.web_search = "live" if enabled else "disabled"
        return self.get_runtime_profile()

    def set_web_search_mode(self, mode: str | None) -> RuntimeProfile:
        if mode is None:
            self._runtime.web_search = None
            return self.get_runtime_profile()
        normalized = mode.strip().lower()
        if normalized not in ALLOWED_WEB_SEARCH_MODES:
            raise RuntimeProfileError(
                f"Invalid web_search mode '{mode}'. Allowed: {', '.join(sorted(ALLOWED_WEB_SEARCH_MODES))}"
            )
        self._runtime.web_search = normalized
        return self.get_runtime_profile()

    def set_personality(self, personality: str, custom_instruction: str | None = None) -> RuntimeProfile:
        normalized = personality.strip().lower()
        if normalized == "custom":
            instruction = (custom_instruction or "").strip()
            if not instruction:
                raise RuntimeProfileError("Custom personality requires an instruction string.")
            self._runtime.personality = "custom"
            self._runtime.personality_instruction = instruction
            return self.get_runtime_profile()

        if normalized not in PERSONALITY_PRESETS:
            raise RuntimeProfileError(
                f"Invalid personality '{personality}'. Allowed: {', '.join(sorted(PERSONALITY_PRESETS))}, custom"
            )
        self._runtime.personality = normalized
        self._runtime.personality_instruction = ""
        return self.get_runtime_profile()

    def set_experimental_feature(self, feature: str, enabled: bool) -> RuntimeProfile:
        normalized = _normalize_feature(feature)
        if not normalized:
            raise RuntimeProfileError("Feature name cannot be empty.")
        if enabled:
            self._runtime.experimental_features.add(normalized)
        else:
            self._runtime.experimental_features.discard(normalized)
        return self.get_runtime_profile()

    def clear_experimental_features(self) -> RuntimeProfile:
        self._runtime.experimental_features.clear()
        return self.get_runtime_profile()

    def set_workdir(self, path_value: str | None) -> RuntimeProfile:
        if path_value is None:
            self._runtime.workdir_override = None
            return self.get_runtime_profile()

        raw = Path(path_value.strip()).expanduser()
        if not str(raw):
            raise RuntimeProfileError("Workdir path cannot be empty.")
        base = self.get_effective_workdir()
        candidate = (base / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise RuntimeProfileError(f"Workdir does not exist or is not a directory: {candidate}")

        allowed = self.get_allowed_workdirs()
        if not any(_is_within(candidate, root) for root in allowed):
            allowed_text = ", ".join(str(p) for p in allowed)
            raise RuntimeProfileError(f"Workdir is outside allowed roots. Allowed: {allowed_text}")

        self._runtime.workdir_override = candidate
        return self.get_runtime_profile()

    def _runtime_cli_flags(self) -> list[str]:
        flags: list[str] = []
        if self._runtime.model:
            flags.append(f"-m {shlex.quote(self._runtime.model)}")
        if self._runtime.reasoning_effort:
            config_value = f'model_reasoning_effort="{self._runtime.reasoning_effort}"'
            flags.append(f"-c {shlex.quote(config_value)}")
        if self._runtime.sandbox_mode:
            flags.append(f"-s {self._runtime.sandbox_mode}")
        approval_policy = self._runtime.approval_policy or self._settings.codex_safe_default_approval
        if approval_policy:
            config_value = f'approval_policy="{approval_policy}"'
            flags.append(f"-c {shlex.quote(config_value)}")
        if self._runtime.web_search:
            config_value = f'web_search="{self._runtime.web_search}"'
            flags.append(f"-c {shlex.quote(config_value)}")
        for feature in sorted(self._runtime.experimental_features):
            flags.append(f"--enable {shlex.quote(feature)}")
        return flags

    def _inject_runtime_flags(self, command: str) -> str:
        flags = self._runtime_cli_flags()
        if not flags:
            return command
        marker = "codex exec "
        if command.startswith(marker):
            return f"{marker}{' '.join(flags)} {command[len(marker):]}".strip()
        if command.strip() == "codex exec":
            return f"codex exec {' '.join(flags)}"
        idx = command.find(marker)
        if idx == -1:
            return command
        start = command[:idx]
        rest = command[idx + len(marker) :]
        return f"{start}{marker}{' '.join(flags)} {rest}".strip()

    def _ensure_skip_git_repo_check(self, command: str) -> str:
        if not self._settings.codex_skip_git_repo_check or not self._settings.codex_auto_safe_flags:
            return command
        if "--skip-git-repo-check" in command:
            return command
        marker = "codex exec "
        if command.startswith(marker):
            return f"{marker}--skip-git-repo-check {command[len(marker):]}".strip()
        if command.strip() == "codex exec":
            return "codex exec --skip-git-repo-check"
        idx = command.find(marker)
        if idx == -1:
            return command
        start = command[:idx]
        rest = command[idx + len(marker) :]
        return f"{start}{marker}--skip-git-repo-check {rest}".strip()

    def _has_output_last_message_flag(self, command: str) -> bool:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return "--output-last-message" in command or " -o " in command

        found_exec = False
        for idx in range(len(tokens) - 1):
            if tokens[idx] != "codex" or tokens[idx + 1] != "exec":
                continue

            found_exec = True
            for token in tokens[idx + 2 :]:
                if token in {"&&", "||", "|", ";"}:
                    break
                if token == "--":
                    break
                if token in {"-o", "--output-last-message"}:
                    return True
                if token.startswith("-o=") or token.startswith("--output-last-message="):
                    return True
        if found_exec:
            return False

        return "--output-last-message" in command or " -o " in command

    def _inject_output_last_message(self, command: str, output_path: Path | None) -> str:
        if output_path is None:
            return command
        if self._has_output_last_message_flag(command):
            return command
        quoted = shlex.quote(str(output_path))
        marker = "codex exec "
        if command.startswith(marker):
            return f"{marker}-o {quoted} {command[len(marker):]}".strip()
        if command.strip() == "codex exec":
            return f"codex exec -o {quoted}"
        idx = command.find(marker)
        if idx == -1:
            return command
        start = command[:idx]
        rest = command[idx + len(marker) :]
        return f"{start}{marker}-o {quoted} {rest}".strip()

    def _apply_personality(self, prompt: str) -> str:
        if self._runtime.personality == "custom":
            instruction = self._runtime.personality_instruction.strip()
        else:
            instruction = PERSONALITY_PRESETS.get(self._runtime.personality, "")
        if not instruction:
            return prompt
        return f"{instruction}\n\n{prompt}"

    def build_plan(self, ctx: ExecutionContext, output_last_message_path: Path | None = None) -> ExecutionPlan:
        prompt = self._apply_personality(ctx.job.prompt)
        template = (
            self._settings.codex_session_cmd_template
            if ctx.job.mode == JobMode.SESSION
            else self._settings.codex_ephemeral_cmd_template
        )
        vars_map = {
            "job_id": str(ctx.job.id),
            "prompt": prompt,
            "prompt_quoted": shlex.quote(prompt),
            "session_name": ctx.job.session_name or "",
            "session_name_quoted": shlex.quote(ctx.job.session_name or ""),
            "approved": "1" if ctx.approved else "0",
            "output_last_message_path": str(output_last_message_path or ""),
            "output_last_message_path_quoted": shlex.quote(str(output_last_message_path or "")),
        }
        command = template.format(**vars_map)
        command = self._inject_runtime_flags(command)
        command = self._ensure_skip_git_repo_check(command)
        command = self._inject_output_last_message(command, output_last_message_path)
        return ExecutionPlan(command=command, env_overrides={"JOB_ID": str(ctx.job.id)})

    async def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = ctx.run_dir / "stdout.log"
        stderr_path = ctx.run_dir / "stderr.log"
        prompt_path = ctx.run_dir / "prompt.txt"
        prompt_path.write_text(ctx.job.prompt, encoding="utf-8")

        last_message_path = ctx.run_dir / "assistant_last_message.txt"
        output_path = last_message_path if self._settings.telegram_response_mode in {"natural", "compact"} else None
        plan = self.build_plan(ctx, output_last_message_path=output_path)
        workdir = self.get_effective_workdir()

        proc: asyncio.subprocess.Process | None = None
        try:
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                proc = await asyncio.create_subprocess_shell(
                    plan.command,
                    cwd=str(workdir),
                    env={**os.environ, **plan.env_overrides},
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=self._settings.job_timeout_seconds)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return ExecutionResult(
                        exit_code=124,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        summary="Timed out while executing Codex command",
                        error_text="Job exceeded timeout limit",
                        exec_cwd=workdir,
                    )

            stdout_tail = _tail_text(stdout_path)
            stderr_tail = _tail_text(stderr_path)
            exit_code = proc.returncode if proc else 1

            if exit_code == 0:
                summary = _read_text(last_message_path)
                if not summary:
                    summary = stdout_tail or "Completed."
                error_text = None
            else:
                summary = "Execution failed."
                error_text = stderr_tail or stdout_tail or "No error output captured"
            return ExecutionResult(
                exit_code=exit_code,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                summary=summary,
                error_text=error_text,
                exec_cwd=workdir,
            )
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
