from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from .executor import RuntimeProfileError
from .models import JobMode, JobStatus
from .orchestrator import Orchestrator
from .sessions import SessionManager
from .video import VideoError, VideoService


HELP_TEXT = """
Commands:
/start - show this help
/run <prompt> - enqueue an ephemeral Codex job
/run_session <session_name> <prompt> - enqueue a session-mode job
/review [scope] - run a code-review style Codex job
/diff [scope] - ask Codex for concise git diff summary
/plan <task> - ask Codex for a detailed implementation plan
/model [name] [reasoning] - show/set model + reasoning effort
/permissions [auto|read-only|full-access|workspace-write|danger-full-access|reset] - set execution permissions
/approvals [untrusted|on-failure|on-request|never|reset] - show/set Codex approval policy
/search [live|cached|disabled|on|off|reset] - show/set web search mode for Codex jobs
/experimental [list|clear|on <feature>|off <feature>] - toggle experimental features
/personality [friendly|pragmatic|none|custom <instruction>] - response style preset
/mcp [list|get <name>] - inspect configured MCP servers
/debug-config - show Codex version + runtime config snapshot
/status - show runtime profile and queue stats
/compact - summarize recent jobs
/jobs - list latest jobs
/job <job_id> - concise job status
/info <job_id> - full diagnostics (workdir/logs/events/artifacts)
/approve <job_id> - approve a waiting job
/reject <job_id> - reject a waiting job
/cancel <job_id> - cancel queued or running job
/video <job_id> - generate and send recap video
/session create <name> - activate a named session
/session stop <name> - stop a named session
/session list - list known sessions
""".strip()


@dataclass(slots=True)
class BotContext:
    owner_user_id: int
    command_cooldown_seconds: float


class CommandGuard:
    def __init__(self, context: BotContext) -> None:
        self._context = context
        self._last_seen: dict[int, float] = {}

    async def authorize(self, message: Message) -> bool:
        user = message.from_user
        if user is None or user.id != self._context.owner_user_id:
            await message.answer("Unauthorized")
            return False
        now = time.monotonic()
        last = self._last_seen.get(user.id)
        if last is not None and now - last < self._context.command_cooldown_seconds:
            wait_for = self._context.command_cooldown_seconds - (now - last)
            await message.answer(f"Rate limited. Retry in {wait_for:.1f}s")
            return False
        self._last_seen[user.id] = now
        return True


def _args(message: Message) -> str:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return ""
    return parts[1].strip()


def _parse_job_id(arg: str) -> int | None:
    try:
        return int(arg)
    except (TypeError, ValueError):
        return None


def _parse_toggle(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"on", "true", "1", "yes", "enable", "enabled"}:
        return True
    if normalized in {"off", "false", "0", "no", "disable", "disabled"}:
        return False
    return None


def _parse_model_payload(payload: str) -> tuple[str, str | None, str | None]:
    trimmed = payload.strip()
    if not trimmed:
        return ("show", None, None)

    parts = trimmed.split()
    first_raw = parts[0]
    first = first_raw.lower()

    if first in {"help", "list"}:
        return ("help", None, None)

    model = None if first in {"default", "reset"} else first_raw
    reasoning: str | None = None

    if len(parts) > 1:
        second = parts[1].strip().lower()
        reasoning = "" if second in {"default", "reset", "none"} else second
    elif first in {"default", "reset"}:
        # `/model reset` should reset both model and reasoning effort.
        reasoning = ""

    return ("set", model, reasoning)


def _model_help_text(orchestrator: Orchestrator) -> str:
    profile = orchestrator.get_runtime_profile()
    return "\n".join(
        [
            "Model settings:",
            f"current_model={profile.model or '(default)'}",
            f"current_reasoning_effort={profile.reasoning_effort or '(default)'}",
            "",
            "Usage:",
            "/model",
            "/model <model_name> [minimal|low|medium|high|xhigh]",
            "/model reset",
            "/model list",
            "",
            "Note: available model names depend on your Codex account/provider.",
        ]
    )


def _parse_feature_catalog_output(output: str) -> list[tuple[str, str, bool]]:
    catalog: list[tuple[str, str, bool]] = []
    pattern = re.compile(r"^([a-zA-Z0-9_\-]+)\s+(.+?)\s+(true|false)$")
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("WARNING:"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        name, stage, effective = match.groups()
        catalog.append((name, stage.strip(), effective == "true"))
    return catalog


def _load_codex_feature_catalog(timeout_seconds: float = 8.0) -> list[tuple[str, str, bool]]:
    try:
        proc = subprocess.run(
            ["codex", "features", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return _parse_feature_catalog_output(proc.stdout + "\n" + proc.stderr)


def _run_codex_capture(args: list[str], timeout_seconds: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["codex", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (1, "", str(exc))
    return (proc.returncode, proc.stdout, proc.stderr)


def _render_experimental_status(orchestrator: Orchestrator, catalog: list[tuple[str, str, bool]]) -> str:
    profile = orchestrator.get_runtime_profile()
    lines = [
        "Experimental controls:",
        "Usage: /experimental <on|off> <feature> | /experimental list | /experimental clear",
        "Enabled for bot runtime: "
        + (", ".join(sorted(profile.experimental_features)) if profile.experimental_features else "(none)"),
    ]
    if catalog:
        lines.append("")
        lines.append("Available in Codex CLI:")
        for name, stage, effective in catalog[:25]:
            lines.append(f"- {name} [{stage}] effective={'on' if effective else 'off'}")
        if len(catalog) > 25:
            lines.append(f"... and {len(catalog) - 25} more")
    else:
        lines.append("")
        lines.append("Could not load feature catalog from `codex features list`.")
    return "\n".join(lines)


def _format_runtime(orchestrator: Orchestrator) -> str:
    profile = orchestrator.get_runtime_profile()
    return "\n".join(
        [
            "Runtime profile:",
            f"model={profile.model or '(default)'}",
            f"reasoning_effort={profile.reasoning_effort or '(default)'}",
            f"permissions={profile.sandbox_mode or '(default)'}",
            f"approvals={profile.approval_policy or '(default)'}",
            f"web_search={profile.web_search or '(default)'}",
            f"personality={profile.personality}",
            "experimental="
            + (", ".join(sorted(profile.experimental_features)) if profile.experimental_features else "(none)"),
        ]
    )


def _tail_file(path: Path, max_chars: int = 2000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text.strip()
    return text[-max_chars:].strip()


def build_dispatcher(
    bot: Bot,
    orchestrator: Orchestrator,
    session_manager: SessionManager,
    video_service: VideoService,
    owner_user_id: int,
    command_cooldown_seconds: float,
    runs_dir: Path,
) -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router()
    guard = CommandGuard(
        BotContext(
            owner_user_id=owner_user_id,
            command_cooldown_seconds=command_cooldown_seconds,
        )
    )

    @router.message(Command("start"))
    async def start_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        await message.answer(HELP_TEXT)

    @router.message(Command("run"))
    async def run_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        prompt = _args(message)
        if not prompt:
            await message.answer("Usage: /run <prompt>")
            return
        job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.EPHEMERAL)
        await message.answer(f"Queued job {job.id} with status {job.status}")

    @router.message(Command("run_session"))
    async def run_session_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            await message.answer("Usage: /run_session <session_name> <prompt>")
            return
        parts = payload.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Usage: /run_session <session_name> <prompt>")
            return
        session_name, prompt = parts[0], parts[1]
        if not session_manager.is_session_active(session_name):
            await message.answer(f"Session '{session_name}' is inactive. Use /session create {session_name}")
            return
        job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.SESSION, session_name=session_name)
        await message.answer(f"Queued session job {job.id} with status {job.status}")

    @router.message(Command("review"))
    async def review_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        scope = _args(message)
        if scope:
            prompt = (
                "Perform a code review on the following scope. Prioritize findings by severity, include file "
                f"references, and propose fixes.\n\nScope: {scope}"
            )
        else:
            prompt = (
                "Perform a code review of current working tree changes. Prioritize findings by severity, include "
                "file references, and highlight missing tests."
            )
        job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.EPHEMERAL)
        await message.answer(f"Queued review job {job.id} with status {job.status}")

    @router.message(Command("diff"))
    async def diff_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        scope = _args(message)
        suffix = f"\n\nFocus scope: {scope}" if scope else ""
        prompt = (
            "Show a concise git diff summary including untracked files. Group by file and call out risky changes."
            f"{suffix}"
        )
        job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.EPHEMERAL)
        await message.answer(f"Queued diff job {job.id} with status {job.status}")

    @router.message(Command("plan"))
    async def plan_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        task = _args(message)
        if not task:
            await message.answer("Usage: /plan <task>")
            return
        prompt = (
            "Create a decision-complete implementation plan with assumptions, APIs, tests, and rollout steps for:\n\n"
            + task
        )
        job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.EPHEMERAL)
        await message.answer(f"Queued planning job {job.id} with status {job.status}")

    @router.message(Command("model"))
    async def model_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        action, model, reasoning = _parse_model_payload(payload)
        if action == "show":
            profile = orchestrator.get_runtime_profile()
            await message.answer(
                f"model={profile.model or '(default)'}\nreasoning_effort={profile.reasoning_effort or '(default)'}"
            )
            return
        if action == "help":
            await message.answer(_model_help_text(orchestrator))
            return
        try:
            updated = orchestrator.set_model(model, reasoning)
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        await message.answer(
            f"Model updated.\nmodel={updated.model or '(default)'}\nreasoning_effort={updated.reasoning_effort or '(default)'}"
        )

    @router.message(Command("permissions"))
    async def permissions_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            profile = orchestrator.get_runtime_profile()
            await message.answer(
                f"permissions={profile.sandbox_mode or '(default)'}\napprovals={profile.approval_policy or '(default)'}"
            )
            return
        mode_arg = payload.strip().lower()
        if mode_arg in {"default", "reset"}:
            mode = None
            approvals = None
        elif mode_arg == "auto":
            mode = "workspace-write"
            approvals = "on-request"
        elif mode_arg == "full-access":
            mode = "danger-full-access"
            approvals = "never"
        elif mode_arg == "read-only":
            mode = "read-only"
            approvals = "on-request"
        else:
            mode = mode_arg
            approvals = None
        try:
            updated = orchestrator.set_permissions(mode)
            if approvals is not None:
                updated = orchestrator.set_approvals(approvals)
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        await message.answer(
            f"Permissions updated: {updated.sandbox_mode or '(default)'}\n"
            f"approvals={updated.approval_policy or '(default)'}"
        )

    @router.message(Command("approvals"))
    async def approvals_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            profile = orchestrator.get_runtime_profile()
            await message.answer(f"approvals={profile.approval_policy or '(default)'}")
            return
        policy_arg = payload.strip().lower()
        policy = None if policy_arg in {"default", "reset"} else policy_arg
        try:
            updated = orchestrator.set_approvals(policy)
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"Approvals updated: {updated.approval_policy or '(default)'}")

    @router.message(Command("search"))
    async def search_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            profile = orchestrator.get_runtime_profile()
            await message.answer(f"web_search={profile.web_search or '(default)'}")
            return
        normalized = payload.strip().lower()
        if normalized in {"reset", "default"}:
            try:
                updated = orchestrator.set_web_search_mode(None)
            except RuntimeProfileError as exc:
                await message.answer(str(exc))
                return
            await message.answer(f"Web search updated: {updated.web_search or '(default)'}")
            return
        enabled = _parse_toggle(normalized)
        mode: str | None = None
        if enabled is not None:
            mode = "live" if enabled else "disabled"
        elif normalized in {"live", "cached", "disabled"}:
            mode = normalized
        else:
            await message.answer("Usage: /search <live|cached|disabled|on|off|reset>")
            return
        try:
            updated = orchestrator.set_web_search_mode(mode)
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"Web search updated: {updated.web_search or '(default)'}")

    @router.message(Command("experimental"))
    async def experimental_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        catalog = await asyncio.to_thread(_load_codex_feature_catalog)
        available_names = {name for name, _, _ in catalog}
        if not payload or payload.strip().lower() == "list":
            await message.answer(_render_experimental_status(orchestrator, catalog))
            return

        parts = payload.split(maxsplit=1)
        action = parts[0].lower()
        if action == "clear":
            orchestrator.clear_experimentals()
            await message.answer("All experimental features cleared.")
            return
        if len(parts) < 2:
            await message.answer("Usage: /experimental <on|off> <feature> or /experimental list|clear")
            return
        feature = parts[1].strip().lower().replace(" ", "-")
        if action not in {"on", "off"}:
            await message.answer("Usage: /experimental <on|off> <feature> or /experimental list|clear")
            return
        if available_names and feature not in available_names:
            closest = sorted(name for name in available_names if feature in name or name in feature)
            suggestion = f"\nDid you mean: {', '.join(closest[:5])}" if closest else ""
            await message.answer(f"Unknown feature `{feature}`.{suggestion}\nUse `/experimental list`.")
            return
        try:
            updated = orchestrator.set_experimental(feature, enabled=(action == "on"))
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        if updated.experimental_features:
            await message.answer("Enabled for bot runtime:\n" + "\n".join(sorted(updated.experimental_features)))
        else:
            await message.answer("Enabled for bot runtime: (none)")

    @router.message(Command("personality"))
    async def personality_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            profile = orchestrator.get_runtime_profile()
            details = (
                f"\ncustom_instruction={profile.personality_instruction[:500]}"
                if profile.personality == "custom"
                else ""
            )
            await message.answer(f"personality={profile.personality}{details}")
            return
        parts = payload.split(maxsplit=1)
        preset = parts[0].lower()
        custom_instruction = parts[1] if len(parts) > 1 else None
        if preset in {"default", "reset"}:
            preset = "none"
            custom_instruction = None
        try:
            updated = orchestrator.set_personality(preset, custom_instruction)
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        await message.answer(f"Personality updated: {updated.personality}")

    @router.message(Command("status"))
    async def status_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        counts = orchestrator.count_jobs_by_status()
        lines = [_format_runtime(orchestrator), ""]
        lines.append("Job counts:")
        for status in sorted(counts):
            lines.append(f"{status}={counts[status]}")
        lines.append(f"running_in_worker={orchestrator.running_jobs_count()}")
        await message.answer("\n".join(lines))

    @router.message(Command("compact"))
    async def compact_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        jobs = orchestrator.list_jobs(limit=30)
        if not jobs:
            await message.answer("No jobs yet.")
            return
        total = len(jobs)
        succeeded = sum(1 for job in jobs if job.status == JobStatus.SUCCEEDED)
        failed = sum(1 for job in jobs if job.status == JobStatus.FAILED)
        waiting = sum(1 for job in jobs if job.status == JobStatus.AWAITING_APPROVAL)
        running = sum(1 for job in jobs if job.status == JobStatus.RUNNING)
        queued = sum(1 for job in jobs if job.status == JobStatus.QUEUED)
        latest = jobs[0]
        lines = [
            f"Compact summary for last {total} jobs:",
            f"succeeded={succeeded}, failed={failed}, waiting_approval={waiting}, running={running}, queued={queued}",
            f"latest_job={latest.id} status={latest.status}",
        ]
        if latest.summary_text:
            lines.append("")
            lines.append(latest.summary_text[:1500])
        await message.answer("\n".join(lines))

    @router.message(Command(commands=["debug-config", "debug_config"]))
    async def debug_config_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        code, stdout, stderr = await asyncio.to_thread(_run_codex_capture, ["--version"])
        version_line = stdout.strip().splitlines()[-1] if code == 0 and stdout.strip() else "unknown"
        if stderr:
            # Hide common non-fatal warning prefix from noisy environments.
            err_lines = [line for line in stderr.splitlines() if not line.startswith("WARNING:")]
            warning = err_lines[-1] if err_lines else ""
        else:
            warning = ""
        lines = [
            f"codex_version={version_line}",
            _format_runtime(orchestrator),
            "",
            "Note: interactive `/debug-config` has no direct non-interactive CLI equivalent in 0.98.0.",
        ]
        if warning:
            lines.append(f"warning={warning[:400]}")
        await message.answer("\n".join(lines))

    @router.message(Command("mcp"))
    async def mcp_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message).strip()
        if not payload or payload.lower() == "list":
            code, stdout, stderr = await asyncio.to_thread(_run_codex_capture, ["mcp", "list", "--json"])
            if code != 0:
                error_line = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
                await message.answer(f"Failed to list MCP servers: {error_line}")
                return
            try:
                servers = json.loads(stdout.strip() or "[]")
            except json.JSONDecodeError:
                await message.answer("Failed to parse MCP list output.")
                return
            if not servers:
                await message.answer("MCP servers: none configured.")
                return
            lines = ["MCP servers:"]
            for server in servers[:20]:
                name = str(server.get("name", "(unnamed)"))
                transport = str(server.get("transport", "unknown"))
                lines.append(f"- {name} ({transport})")
            await message.answer("\n".join(lines))
            return

        if payload.lower().startswith("get "):
            name = payload[4:].strip()
            if not name:
                await message.answer("Usage: /mcp get <name>")
                return
            code, stdout, stderr = await asyncio.to_thread(_run_codex_capture, ["mcp", "get", name, "--json"])
            if code != 0:
                error_line = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
                await message.answer(f"Failed to get MCP server `{name}`: {error_line}")
                return
            await message.answer(stdout.strip()[:3500] or "{}")
            return

        await message.answer("Usage: /mcp [list] | /mcp get <name>")

    @router.message(
        Command(
            commands=[
                "skills",
                "new",
                "resume",
                "fork",
                "agent",
                "collab",
                "apps",
                "rename",
                "mention",
                "init",
                "ps",
                "feedback",
                "logout",
                "exit",
            ]
        )
    )
    async def compatibility_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        command_name = (message.text or "").split(maxsplit=1)[0]
        await message.answer(
            f"{command_name} is an interactive Codex CLI command and is not fully mapped in Telegram yet.\n"
            "Available Telegram controls: /model, /permissions, /approvals, /search, /experimental, "
            "/personality, /status, /compact, /review, /diff, /plan."
        )

    @router.message(Command("jobs"))
    async def jobs_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        jobs = orchestrator.list_jobs(limit=20)
        if not jobs:
            await message.answer("No jobs yet")
            return
        lines = ["Latest jobs:"]
        for job in jobs:
            lines.append(f"{job.id}: {job.status} mode={job.mode} risk={job.risk_level}")
        await message.answer("\n".join(lines))

    @router.message(Command("job"))
    async def job_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        job_id = _parse_job_id(_args(message))
        if job_id is None:
            await message.answer("Usage: /job <job_id>")
            return
        try:
            job = orchestrator.get_job(job_id)
        except KeyError:
            await message.answer(f"Job {job_id} not found")
            return
        if job.status == JobStatus.SUCCEEDED and job.summary_text:
            await message.answer(job.summary_text[:3500])
            return

        lines = [f"job={job.id}", f"status={job.status}"]
        if job.status == JobStatus.FAILED:
            lines.append("Use /info <job_id> for diagnostics.")
            if job.error_text:
                lines.append(f"error={job.error_text[:800]}")
        elif job.summary_text:
            lines.append(job.summary_text[:1200])
        await message.answer("\n".join(lines))

    @router.message(Command("info"))
    async def info_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        job_id = _parse_job_id(_args(message))
        if job_id is None:
            await message.answer("Usage: /info <job_id>")
            return
        try:
            job = orchestrator.get_job(job_id)
        except KeyError:
            await message.answer(f"Job {job_id} not found")
            return

        artifacts = orchestrator.list_job_artifacts(job_id)
        events = orchestrator.list_job_events(job_id, limit=10)
        run_dir = runs_dir / str(job_id)
        stdout_tail = _tail_file(run_dir / "stdout.log")
        stderr_tail = _tail_file(run_dir / "stderr.log")

        lines = [
            f"job={job.id}",
            f"status={job.status}",
            f"mode={job.mode}",
            f"risk={job.risk_level}",
            f"needs_approval={job.needs_approval}",
            f"workdir={run_dir}",
            f"artifacts={len(artifacts)}",
        ]
        if job.session_name:
            lines.append(f"session={job.session_name}")
        if job.exit_code is not None:
            lines.append(f"exit_code={job.exit_code}")
        if artifacts:
            lines.append("artifact_files=" + ", ".join(a.path.name for a in artifacts[:10]))
        if events:
            lines.append("recent_events=" + ", ".join(event_type for _, event_type, _ in events))
        if stdout_tail:
            lines.append("")
            lines.append("stdout tail:")
            lines.append(stdout_tail)
        if stderr_tail:
            lines.append("")
            lines.append("stderr tail:")
            lines.append(stderr_tail)
        await message.answer("\n".join(lines))

    @router.message(Command("approve"))
    async def approve_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        job_id = _parse_job_id(_args(message))
        if job_id is None:
            await message.answer("Usage: /approve <job_id>")
            return
        user = message.from_user
        assert user is not None
        try:
            job = await orchestrator.approve_job(job_id, user.id)
        except KeyError:
            await message.answer(f"Job {job_id} not found")
            return
        if job.status != JobStatus.QUEUED:
            await message.answer(f"Job {job_id} was not awaiting approval (status={job.status})")
            return
        await message.answer(f"Approved job {job_id}; it is queued")

    @router.message(Command("reject"))
    async def reject_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        job_id = _parse_job_id(_args(message))
        if job_id is None:
            await message.answer("Usage: /reject <job_id>")
            return
        user = message.from_user
        assert user is not None
        try:
            job = await orchestrator.reject_job(job_id, user.id)
        except KeyError:
            await message.answer(f"Job {job_id} not found")
            return
        await message.answer(f"Rejected job {job.id}; status={job.status}")

    @router.message(Command("cancel"))
    async def cancel_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        job_id = _parse_job_id(_args(message))
        if job_id is None:
            await message.answer("Usage: /cancel <job_id>")
            return
        try:
            job = await orchestrator.cancel_job(job_id)
        except KeyError:
            await message.answer(f"Job {job_id} not found")
            return
        await message.answer(f"Cancel requested for job {job.id}; status={job.status}")

    @router.message(Command("video"))
    async def video_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        job_id = _parse_job_id(_args(message))
        if job_id is None:
            await message.answer("Usage: /video <job_id>")
            return
        try:
            artifact = await video_service.generate_for_job(job_id)
        except KeyError:
            await message.answer(f"Job {job_id} not found")
            return
        except VideoError as exc:
            await message.answer(f"Video generation failed: {exc}")
            return

        await message.answer("Video created. Uploading...")
        await bot.send_document(
            chat_id=owner_user_id,
            document=FSInputFile(artifact.path),
            caption=f"job={job_id} recap video",
        )

    @router.message(Command("session"))
    async def session_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        parts = payload.split()
        subcommand = parts[0].lower() if parts else "list"

        if subcommand == "list":
            sessions = session_manager.list_sessions()
            if not sessions:
                await message.answer("No sessions")
                return
            lines = ["Sessions:"]
            for session in sessions:
                lines.append(f"{session.name}: {session.status} pid={session.pid}")
            await message.answer("\n".join(lines))
            return

        if len(parts) < 2:
            await message.answer("Usage: /session <create|stop> <name>")
            return
        name = parts[1]

        if subcommand == "create":
            try:
                result = await session_manager.create(name)
            except Exception as exc:
                await message.answer(f"Failed to create session: {exc}")
                return
            status = "created" if result.created else "already active"
            await message.answer(f"Session {name}: {status}")
            return

        if subcommand == "stop":
            try:
                record = await session_manager.stop(name)
            except KeyError:
                await message.answer(f"Session {name} not found")
                return
            await message.answer(f"Session {name} stopped (status={record.status})")
            return

        await message.answer("Unknown session subcommand. Use create|stop|list")

    @router.message()
    async def fallback_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        await message.answer("Unknown command. Use /start for help.")

    dispatcher.include_router(router)
    return dispatcher
