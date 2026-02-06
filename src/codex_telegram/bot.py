from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shlex
import subprocess
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message, PollAnswer

from .approval_checklists import (
    APPROVAL_TASK_APPROVE,
    APPROVAL_TASK_REJECT,
    APPROVAL_TASK_REVISE,
    ApprovalChecklist,
    ApprovalChecklistStore,
)
from .approval_polls import (
    APPROVAL_OPTION_APPROVE,
    APPROVAL_OPTION_REJECT,
    APPROVAL_OPTION_REVISE,
    ApprovalPoll,
    ApprovalPollStore,
)
from .assistant_polls import AssistantPoll, AssistantPollStore
from .executor import RuntimeProfileError
from .models import JobMode, JobStatus
from .orchestrator import Orchestrator
from .sessions import SessionManager
from .video import VideoError, VideoService


HELP_TEXT = """
Commands:
/start - show this help
/run <prompt> - enqueue a job (uses active session when set)
send file or image (optional caption or /run caption) - enqueue attachment-aware job
/run_session <session_name> <prompt> - enqueue a job in explicit session mode
/new [name] - create a session and set it as active for this chat
/resume <session_name_or_id> - activate/resume a session for this chat
/fork [source_session] - create a forked session and activate it
/session [list|create|stop|use|clear] [name] - manage sessions and active session pointer
/agent [list|switch <name>] - lightweight agent profile routing hint
/mention <path> <prompt> - run prompt with explicit file mention context
/init [extra instructions] - scaffold an AGENTS.md style instruction prompt via Codex
/review [scope] - run a code-review style Codex job
/diff [scope] - ask Codex for concise git diff summary
/plan <task> - ask Codex for a detailed implementation plan
/poll [question | option1 | option2 ...] - send a manual test poll
/model [name] [reasoning] - show/set model + reasoning effort
/permissions [auto|read-only|full-access|workspace-write|danger-full-access|reset] - set execution permissions
/approvals [untrusted|on-failure|on-request|never|reset] - show/set Codex approval policy
/search [live|cached|disabled|on|off|reset] - show/set web search mode for Codex jobs
/workdir [show|set <path>|reset] - show/set Codex working directory (allowlist enforced)
/experimental [list|clear|on <feature>|off <feature>] - toggle experimental features
/personality [friendly|pragmatic|none|custom <instruction>] - response style preset
/mcp [list|get <name>] - inspect configured MCP servers
/debug-config - show Codex version + runtime config snapshot
/status - show runtime profile and queue stats
/compact - summarize recent jobs
/jobs - list latest jobs
/job <job_id> - concise job status
/info <job_id> - full diagnostics (workdir/logs/events/artifacts)
/approve <job_id> - approve a waiting job (poll/checklist fallback)
/reject <job_id> - reject a waiting job (poll/checklist fallback)
/cancel <job_id> - cancel queued or running job
/video <job_id> - generate and send recap video
""".strip()


@dataclass(slots=True)
class BotContext:
    owner_user_id: int
    command_cooldown_seconds: float


@dataclass(slots=True)
class _IncomingAttachment:
    kind: str
    path: Path


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
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _split_args(payload: str) -> list[str]:
    if not payload.strip():
        return []
    try:
        return shlex.split(payload)
    except ValueError:
        return payload.split()


def _chat_id(message: Message) -> int:
    if message.chat is not None:
        return int(message.chat.id)
    user = message.from_user
    if user is None:
        raise RuntimeError("Message has no chat or user")
    return int(user.id)


def _auto_session_name(prefix: str = "session") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}"


def _sanitize_session_token(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "session"


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

    if first == "help":
        return ("help", None, None)
    if first == "list":
        return ("list", None, None)

    model = None if first in {"default", "reset"} else first_raw
    reasoning: str | None = None

    if len(parts) > 1:
        second = parts[1].strip().lower()
        reasoning = "" if second in {"default", "reset", "none"} else second
    elif first in {"default", "reset"}:
        # `/model reset` should reset both model and reasoning effort.
        reasoning = ""

    return ("set", model, reasoning)


def _codex_config_path() -> Path:
    codex_home = os.getenv("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _load_codex_runtime_defaults() -> dict[str, str]:
    path = _codex_config_path()
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    profile_overrides: dict[str, object] = {}
    profile_name = os.getenv("CODEX_PROFILE", "").strip()
    if profile_name:
        profiles = data.get("profiles")
        if isinstance(profiles, dict):
            selected = profiles.get(profile_name)
            if isinstance(selected, dict):
                profile_overrides = selected

    def _from_config(*keys: str) -> str | None:
        for key in keys:
            value: object | None
            if key in profile_overrides:
                value = profile_overrides.get(key)
            else:
                value = data.get(key)
            if isinstance(value, bool):
                if key == "web_search":
                    return "live" if value else "disabled"
                return "true" if value else "false"
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    model = _from_config("model")
    reasoning = _from_config("model_reasoning_effort")
    sandbox_mode = _from_config("sandbox_mode", "sandbox")
    approval_policy = _from_config("approval_policy", "ask_for_approval")
    web_search = _from_config("web_search")
    out: dict[str, str] = {}
    if model:
        out["model"] = model
    if reasoning:
        out["reasoning_effort"] = reasoning
    if sandbox_mode:
        out["sandbox_mode"] = sandbox_mode
    if approval_policy:
        out["approval_policy"] = approval_policy
    if web_search:
        out["web_search"] = web_search
    return out


def _render_runtime_value(
    current: str | None,
    configured: str | None,
    *,
    unknown_text: str,
) -> str:
    if current:
        return current
    if configured:
        return f"{configured} (from Codex config)"
    return unknown_text


def _model_help_text(orchestrator: Orchestrator) -> str:
    profile = orchestrator.get_runtime_profile()
    defaults = _load_codex_runtime_defaults()
    return "\n".join(
        [
            "Model settings:",
            "current_model="
            + _render_runtime_value(
                profile.model,
                defaults.get("model"),
                unknown_text="unknown (set explicitly with /model <name>)",
            ),
            "current_reasoning_effort="
            + _render_runtime_value(
                profile.reasoning_effort,
                defaults.get("reasoning_effort"),
                unknown_text="auto (provider-managed)",
            ),
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
    defaults = _load_codex_runtime_defaults()
    effective_approvals = orchestrator.get_effective_approval_policy()
    allowed_roots = ", ".join(str(p) for p in orchestrator.get_allowed_workdirs())
    return "\n".join(
        [
            "Runtime profile:",
            "model="
            + _render_runtime_value(
                profile.model,
                defaults.get("model"),
                unknown_text="unknown (set explicitly with /model <name>)",
            ),
            "reasoning_effort="
            + _render_runtime_value(
                profile.reasoning_effort,
                defaults.get("reasoning_effort"),
                unknown_text="auto (provider-managed)",
            ),
            "permissions="
            + _render_runtime_value(
                profile.sandbox_mode,
                defaults.get("sandbox_mode"),
                unknown_text="Codex CLI internal value (not configured)",
            ),
            "approvals="
            + _render_runtime_value(
                profile.approval_policy,
                defaults.get("approval_policy") or effective_approvals,
                unknown_text=effective_approvals,
            ),
            "web_search="
            + _render_runtime_value(
                profile.web_search,
                defaults.get("web_search"),
                unknown_text="Codex CLI internal value (not configured)",
            ),
            f"personality={profile.personality}",
            f"workdir={orchestrator.get_effective_workdir()}",
            f"allowed_workdirs={allowed_roots}",
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


def _has_supported_attachments(message: Message) -> bool:
    return bool(message.document or message.photo)


def _attachment_prompt_from_message(message: Message) -> str | None:
    raw = (message.caption or message.text or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if not lowered.startswith("/"):
        return raw
    if lowered == "/run":
        return ""
    if lowered.startswith("/run "):
        return raw[5:].strip()
    return None


def _sanitize_filename(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    return candidate or fallback


def _unique_path(directory: Path, filename: str) -> Path:
    base = Path(filename)
    stem = base.stem or "upload"
    suffix = base.suffix
    candidate = directory / filename
    idx = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{idx}{suffix}"
        idx += 1
    return candidate


async def _download_message_attachments(
    message: Message,
    bot: Bot,
    workdir: Path,
) -> list[_IncomingAttachment]:
    upload_root = workdir / ".codex_telegram_uploads"
    upload_dir = upload_root / f"chat-{_chat_id(message)}" / f"message-{int(message.message_id)}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    attachments: list[_IncomingAttachment] = []

    document = message.document
    if document is not None:
        file_name = (document.file_name or "").strip()
        if not file_name:
            ext = mimetypes.guess_extension(document.mime_type or "") or ""
            file_name = f"document-{document.file_unique_id}{ext}"
        safe_name = _sanitize_filename(file_name, f"document-{document.file_unique_id}")
        target = _unique_path(upload_dir, safe_name)
        await bot.download(document.file_id, destination=target)
        kind = "image" if (document.mime_type or "").startswith("image/") else "file"
        attachments.append(_IncomingAttachment(kind=kind, path=target))

    if message.photo:
        largest = message.photo[-1]
        target = _unique_path(upload_dir, f"photo-{largest.file_unique_id}.jpg")
        await bot.download(largest.file_id, destination=target)
        attachments.append(_IncomingAttachment(kind="image", path=target))

    return attachments


def _build_attachment_prompt(user_prompt: str, attachments: list[_IncomingAttachment]) -> str:
    lines = ["Telegram attachments saved in workspace:"]
    for attachment in attachments:
        lines.append(f"- {attachment.kind}: {attachment.path}")
    lines.append("")
    if user_prompt:
        lines.append("User request:")
        lines.append(user_prompt)
    else:
        lines.append(
            "No extra user prompt was provided. Inspect these attachments, summarize what they contain, "
            "and ask for clarification if the next action is ambiguous."
        )
    return "\n".join(lines)


def build_dispatcher(
    bot: Bot,
    orchestrator: Orchestrator,
    session_manager: SessionManager,
    video_service: VideoService,
    owner_user_id: int,
    command_cooldown_seconds: float,
    runs_dir: Path,
    approval_polls: ApprovalPollStore | None = None,
    approval_checklists: ApprovalChecklistStore | None = None,
    assistant_polls: AssistantPollStore | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router()
    chat_agents: dict[int, str] = {}
    active_approval_polls = approval_polls or ApprovalPollStore()
    active_approval_checklists = approval_checklists or ApprovalChecklistStore()
    active_assistant_polls = assistant_polls or AssistantPollStore()
    guard = CommandGuard(
        BotContext(
            owner_user_id=owner_user_id,
            command_cooldown_seconds=command_cooldown_seconds,
        )
    )

    async def _close_approval_poll(poll: ApprovalPoll) -> None:
        try:
            await bot.stop_poll(chat_id=poll.chat_id, message_id=poll.message_id)
        except Exception:
            return

    async def _close_assistant_poll(poll: AssistantPoll) -> None:
        try:
            await bot.stop_poll(chat_id=poll.chat_id, message_id=poll.message_id)
        except Exception:
            return

    async def _close_approval_poll_for_job(job_id: int) -> None:
        poll = active_approval_polls.pop_for_job(job_id)
        if poll is None:
            return
        await _close_approval_poll(poll)

    def _drop_approval_checklist_for_job(job_id: int) -> ApprovalChecklist | None:
        return active_approval_checklists.pop_for_job(job_id)

    async def _clear_approval_ui_for_job(job_id: int) -> None:
        _drop_approval_checklist_for_job(job_id)
        await _close_approval_poll_for_job(job_id)

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
        chat_id = _chat_id(message)
        active_session = orchestrator.get_active_session_for_chat(chat_id)
        if active_session:
            if not session_manager.is_session_active(active_session):
                await message.answer(
                    f"Active session `{active_session}` is inactive. Use /resume {active_session} or /session clear."
                )
                return
            agent_hint = chat_agents.get(chat_id)
            effective_prompt = (
                f"Use agent profile `{agent_hint}` for this response.\n\n{prompt}" if agent_hint else prompt
            )
            job = await orchestrator.submit_job(
                prompt=effective_prompt,
                mode=JobMode.SESSION,
                session_name=active_session,
            )
            await message.answer(f"Queued session job {job.id} in `{active_session}` ({job.status})")
            return

        agent_hint = chat_agents.get(chat_id)
        effective_prompt = f"Use agent profile `{agent_hint}` for this response.\n\n{prompt}" if agent_hint else prompt
        job = await orchestrator.submit_job(prompt=effective_prompt, mode=JobMode.EPHEMERAL)
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
        orchestrator.set_active_session_for_chat(_chat_id(message), session_name)
        await message.answer(f"Queued session job {job.id} with status {job.status}")

    @router.message(Command("new"))
    async def new_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message).strip()
        raw_name = payload or _auto_session_name("session")
        name = _sanitize_session_token(raw_name)
        try:
            result = await session_manager.create(name)
        except Exception as exc:
            await message.answer(f"Failed to create session: {exc}")
            return
        chat_id = _chat_id(message)
        orchestrator.set_active_session_for_chat(chat_id, name)
        status = "created" if result.created else "already active"
        await message.answer(f"Session {name}: {status}\nActive session set to `{name}`.")

    @router.message(Command("resume"))
    async def resume_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        session_name = _args(message).strip()
        if not session_name:
            await message.answer("Usage: /resume <session_name_or_id>")
            return
        session_name = _sanitize_session_token(session_name)
        try:
            await session_manager.create(session_name)
        except Exception as exc:
            await message.answer(f"Failed to resume session: {exc}")
            return
        chat_id = _chat_id(message)
        orchestrator.set_active_session_for_chat(chat_id, session_name)
        await message.answer(f"Active session set to `{session_name}`.")

    @router.message(Command("fork"))
    async def fork_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        chat_id = _chat_id(message)
        payload = _args(message).strip()
        source = _sanitize_session_token(payload) if payload else orchestrator.get_active_session_for_chat(chat_id)
        if not source:
            await message.answer("Usage: /fork <source_session>\nTip: set one first with /new or /resume.")
            return
        fork_name = _sanitize_session_token(f"{source}-fork-{datetime.now(UTC).strftime('%H%M%S')}")
        try:
            await session_manager.create(fork_name)
        except Exception as exc:
            await message.answer(f"Failed to fork session: {exc}")
            return
        orchestrator.set_active_session_for_chat(chat_id, fork_name)
        await message.answer(f"Forked `{source}` -> `{fork_name}` and set it as active.")

    @router.message(Command("agent"))
    async def agent_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        chat_id = _chat_id(message)
        payload = _args(message).strip()
        if not payload or payload.lower() == "list":
            active = chat_agents.get(chat_id) or "builtin"
            await message.answer(f"Agents:\n- builtin\nActive agent={active}\nUsage: /agent switch <name>")
            return
        parts = _split_args(payload)
        action = parts[0].lower() if parts else ""
        if action == "switch" and len(parts) >= 2:
            name = _sanitize_session_token(parts[1])
            chat_agents[chat_id] = name
            await message.answer(f"Active agent set to `{name}` for this chat.")
            return
        if action in {"reset", "clear"}:
            chat_agents.pop(chat_id, None)
            await message.answer("Active agent reset to default.")
            return
        await message.answer("Usage: /agent [list|switch <name>|reset]")

    @router.message(Command("mention"))
    async def mention_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message).strip()
        parts = _split_args(payload)
        if len(parts) < 2:
            await message.answer("Usage: /mention <path> <prompt>")
            return
        mention_path = parts[0]
        task_prompt = " ".join(parts[1:]).strip()
        prompt = f"Use context from `{mention_path}` in the current workdir while answering.\n\n{task_prompt}"
        chat_id = _chat_id(message)
        active_session = orchestrator.get_active_session_for_chat(chat_id)
        if active_session and session_manager.is_session_active(active_session):
            job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.SESSION, session_name=active_session)
            await message.answer(f"Queued session mention job {job.id} in `{active_session}` ({job.status})")
            return
        job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.EPHEMERAL)
        await message.answer(f"Queued mention job {job.id} with status {job.status}")

    @router.message(Command("init"))
    async def init_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        extra = _args(message).strip()
        prompt = (
            "Create or refresh an AGENTS.md file for this repository. Keep it concise and practical for Codex use.\n"
            "Include: code style rules, testing workflow, and safe execution constraints."
        )
        if extra:
            prompt += f"\n\nExtra requirements:\n{extra}"
        job = await orchestrator.submit_job(prompt=prompt, mode=JobMode.EPHEMERAL)
        await message.answer(f"Queued init job {job.id} with status {job.status}")

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

    @router.message(Command("poll"))
    async def poll_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message).strip()
        question = "Poll feature smoke test: does this new poll flow work?"
        options: tuple[str, ...] = ("Yes, works", "Partially works", "No, needs fixes")

        if payload:
            raw_parts = [part.strip() for part in payload.split("|")]
            parts = [part for part in raw_parts if part]
            if len(parts) < 3:
                await message.answer("Usage: /poll [question | option1 | option2 | option3 ...]")
                return
            question = parts[0][:300]
            deduped = list(dict.fromkeys(option[:100] for option in parts[1:] if option))
            if len(deduped) < 2:
                await message.answer("Poll must include at least 2 distinct non-empty options.")
                return
            options = tuple(deduped[:10])

        try:
            sent = await bot.send_poll(
                chat_id=_chat_id(message),
                question=question,
                options=list(options),
                is_anonymous=False,
                allows_multiple_answers=False,
            )
        except Exception as exc:
            await message.answer(f"Failed to send poll: {exc}")
            return

        if sent.poll is None or not sent.poll.id:
            await message.answer("Poll sent, but response did not include poll metadata.")
            return

        active_assistant_polls.register(
            AssistantPoll(
                poll_id=sent.poll.id,
                source_job_id=int(sent.message_id),
                chat_id=_chat_id(message),
                message_id=int(sent.message_id),
                question=question,
                options=options,
                allows_multiple_answers=False,
            )
        )
        await message.answer("Test poll created. Vote in the poll to trigger the poll-answer flow.")

    @router.message(Command("model"))
    async def model_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        action, model, reasoning = _parse_model_payload(payload)
        if action == "show":
            profile = orchestrator.get_runtime_profile()
            defaults = _load_codex_runtime_defaults()
            await message.answer(
                "model="
                + _render_runtime_value(
                    profile.model,
                    defaults.get("model"),
                    unknown_text="unknown (set explicitly with /model <name>)",
                )
                + "\nreasoning_effort="
                + _render_runtime_value(
                    profile.reasoning_effort,
                    defaults.get("reasoning_effort"),
                    unknown_text="auto (provider-managed)",
                )
            )
            return
        if action == "help":
            await message.answer(_model_help_text(orchestrator))
            return
        if action == "list":
            code, stdout, stderr = await asyncio.to_thread(_run_codex_capture, ["exec", "-h"])
            if code != 0:
                error_line = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
                await message.answer(f"Unable to inspect model options from Codex CLI: {error_line}")
                return
            hint = "Codex CLI does not expose a non-interactive model catalog; use model names from your account."
            if "--model <MODEL>" in stdout:
                hint = (
                    "Model names are account/provider specific.\n"
                    "Use `/model <name>` with a known model (example: `/model gpt-5-codex high`)."
                )
            await message.answer(hint)
            return
        try:
            updated = orchestrator.set_model(model, reasoning)
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        defaults = _load_codex_runtime_defaults()
        await message.answer(
            "Model updated.\n"
            "model="
            + _render_runtime_value(
                updated.model,
                defaults.get("model"),
                unknown_text="unknown (set explicitly with /model <name>)",
            )
            + "\nreasoning_effort="
            + _render_runtime_value(
                updated.reasoning_effort,
                defaults.get("reasoning_effort"),
                unknown_text="auto (provider-managed)",
            )
        )

    @router.message(Command("permissions"))
    async def permissions_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            profile = orchestrator.get_runtime_profile()
            defaults = _load_codex_runtime_defaults()
            await message.answer(
                "permissions="
                + _render_runtime_value(
                    profile.sandbox_mode,
                    defaults.get("sandbox_mode"),
                    unknown_text="Codex CLI internal value (not configured)",
                )
                + "\napprovals="
                + _render_runtime_value(
                    profile.approval_policy,
                    defaults.get("approval_policy") or orchestrator.get_effective_approval_policy(),
                    unknown_text=orchestrator.get_effective_approval_policy(),
                )
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
        defaults = _load_codex_runtime_defaults()
        await message.answer(
            "Permissions updated: "
            + _render_runtime_value(
                updated.sandbox_mode,
                defaults.get("sandbox_mode"),
                unknown_text="Codex CLI internal value (not configured)",
            )
            + "\napprovals="
            + _render_runtime_value(
                updated.approval_policy,
                defaults.get("approval_policy") or orchestrator.get_effective_approval_policy(),
                unknown_text=orchestrator.get_effective_approval_policy(),
            )
        )

    @router.message(Command("approvals"))
    async def approvals_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            profile = orchestrator.get_runtime_profile()
            defaults = _load_codex_runtime_defaults()
            await message.answer(
                "approvals="
                + _render_runtime_value(
                    profile.approval_policy,
                    defaults.get("approval_policy") or orchestrator.get_effective_approval_policy(),
                    unknown_text=orchestrator.get_effective_approval_policy(),
                )
            )
            return
        policy_arg = payload.strip().lower()
        policy = None if policy_arg in {"default", "reset"} else policy_arg
        try:
            updated = orchestrator.set_approvals(policy)
        except RuntimeProfileError as exc:
            await message.answer(str(exc))
            return
        defaults = _load_codex_runtime_defaults()
        await message.answer(
            "Approvals updated: "
            + _render_runtime_value(
                updated.approval_policy,
                defaults.get("approval_policy") or orchestrator.get_effective_approval_policy(),
                unknown_text=orchestrator.get_effective_approval_policy(),
            )
        )

    @router.message(Command("search"))
    async def search_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message)
        if not payload:
            profile = orchestrator.get_runtime_profile()
            defaults = _load_codex_runtime_defaults()
            await message.answer(
                "web_search="
                + _render_runtime_value(
                    profile.web_search,
                    defaults.get("web_search"),
                    unknown_text="Codex CLI internal value (not configured)",
                )
            )
            return
        normalized = payload.strip().lower()
        if normalized in {"reset", "default"}:
            try:
                updated = orchestrator.set_web_search_mode(None)
            except RuntimeProfileError as exc:
                await message.answer(str(exc))
                return
            defaults = _load_codex_runtime_defaults()
            await message.answer(
                "Web search updated: "
                + _render_runtime_value(
                    updated.web_search,
                    defaults.get("web_search"),
                    unknown_text="Codex CLI internal value (not configured)",
                )
            )
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
        defaults = _load_codex_runtime_defaults()
        await message.answer(
            "Web search updated: "
            + _render_runtime_value(
                updated.web_search,
                defaults.get("web_search"),
                unknown_text="Codex CLI internal value (not configured)",
            )
        )

    @router.message(Command("workdir"))
    async def workdir_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        payload = _args(message).strip()
        current = orchestrator.get_effective_workdir()
        allowed = orchestrator.get_allowed_workdirs()
        allowed_text = ", ".join(str(p) for p in allowed)

        if not payload or payload.lower() in {"show", "list"}:
            await message.answer(
                f"workdir={current}\nallowed_roots={allowed_text}\nUsage: /workdir set <path> | /workdir reset"
            )
            return

        if payload.lower() in {"reset", "default"}:
            try:
                orchestrator.set_workdir(None)
            except RuntimeProfileError as exc:
                await message.answer(str(exc))
                return
            await message.answer(f"Workdir reset to default: {orchestrator.get_effective_workdir()}")
            return

        if payload.lower().startswith("set "):
            path_value = payload[4:].strip()
            if not path_value:
                await message.answer("Usage: /workdir set <path>")
                return
            try:
                orchestrator.set_workdir(path_value)
            except RuntimeProfileError as exc:
                await message.answer(str(exc))
                return
            await message.answer(f"Workdir updated: {orchestrator.get_effective_workdir()}")
            return

        await message.answer("Usage: /workdir [show|set <path>|reset]")

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
        chat_id = _chat_id(message)
        counts = orchestrator.count_jobs_by_status()
        lines = [_format_runtime(orchestrator), ""]
        lines.append(f"active_session_for_chat={orchestrator.get_active_session_for_chat(chat_id) or '(none)'}")
        lines.append(f"active_agent_for_chat={chat_agents.get(chat_id) or 'builtin'}")
        lines.append("")
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
                "collab",
                "apps",
                "rename",
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
            "/personality, /status, /compact, /review, /diff, /plan, /new, /resume, /fork."
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
        await _clear_approval_ui_for_job(job_id)
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
        await _clear_approval_ui_for_job(job_id)
        await message.answer(f"Rejected job {job.id}; status={job.status}")

    @router.poll_answer()
    async def poll_approval_handler(answer: PollAnswer) -> None:
        user = answer.user
        if user is None or user.id != owner_user_id:
            return
        approval_poll = active_approval_polls.get(answer.poll_id)
        if approval_poll is not None:
            if not answer.option_ids:
                return
            selected = int(answer.option_ids[0])
            if selected not in {APPROVAL_OPTION_APPROVE, APPROVAL_OPTION_REJECT, APPROVAL_OPTION_REVISE}:
                return
            active_approval_polls.pop(answer.poll_id)

            if selected == APPROVAL_OPTION_APPROVE:
                try:
                    job = await orchestrator.approve_job(approval_poll.job_id, user.id)
                except KeyError:
                    await bot.send_message(chat_id=owner_user_id, text=f"Job {approval_poll.job_id} not found")
                    return
                finally:
                    _drop_approval_checklist_for_job(approval_poll.job_id)
                    await _close_approval_poll(approval_poll)
                if job.status != JobStatus.QUEUED:
                    await bot.send_message(
                        chat_id=owner_user_id,
                        text=f"Job {approval_poll.job_id} was not awaiting approval (status={job.status})",
                    )
                    return
                await bot.send_message(chat_id=owner_user_id, text=f"Approved job {approval_poll.job_id}; it is queued")
                return

            try:
                job = await orchestrator.reject_job(approval_poll.job_id, user.id)
            except KeyError:
                await bot.send_message(chat_id=owner_user_id, text=f"Job {approval_poll.job_id} not found")
                return
            finally:
                _drop_approval_checklist_for_job(approval_poll.job_id)
                await _close_approval_poll(approval_poll)
            if job.status != JobStatus.REJECTED:
                await bot.send_message(
                    chat_id=owner_user_id,
                    text=f"Job {approval_poll.job_id} was not awaiting approval (status={job.status})",
                )
                return
            if selected == APPROVAL_OPTION_REJECT:
                await bot.send_message(chat_id=owner_user_id, text=f"Rejected job {approval_poll.job_id}; status={job.status}")
                return
            await bot.send_message(
                chat_id=owner_user_id,
                text=f"Job {approval_poll.job_id} marked rejected. Send a revised prompt with /run when ready.",
            )
            return

        assistant_poll = active_assistant_polls.pop(answer.poll_id)
        if assistant_poll is None or not answer.option_ids:
            return
        await _close_assistant_poll(assistant_poll)
        selected_options: list[str] = []
        for raw_idx in answer.option_ids:
            idx = int(raw_idx)
            if 0 <= idx < len(assistant_poll.options):
                selected_options.append(assistant_poll.options[idx])
        if not selected_options:
            await bot.send_message(chat_id=owner_user_id, text="Poll answer had no valid options.")
            return
        selected_text = ", ".join(selected_options)
        followup_prompt = (
            f"The user answered your poll for job {assistant_poll.source_job_id}.\n"
            f"Question: {assistant_poll.question}\n"
            f"Selected option(s): {selected_text}\n\n"
            "Continue from this decision and perform the next concrete step."
        )
        chat_id = assistant_poll.chat_id
        active_session = orchestrator.get_active_session_for_chat(chat_id)
        if active_session and session_manager.is_session_active(active_session):
            job = await orchestrator.submit_job(
                prompt=followup_prompt,
                mode=JobMode.SESSION,
                session_name=active_session,
            )
            await bot.send_message(
                chat_id=owner_user_id,
                text=f"Poll response saved: {selected_text}\nQueued session follow-up job {job.id} in `{active_session}`.",
            )
            return

        job = await orchestrator.submit_job(prompt=followup_prompt, mode=JobMode.EPHEMERAL)
        await bot.send_message(
            chat_id=owner_user_id,
            text=f"Poll response saved: {selected_text}\nQueued follow-up job {job.id}.",
        )

    async def _handle_checklist_approval_message(message: Message) -> bool:
        event = message.checklist_tasks_done
        if event is None:
            return False
        user = message.from_user
        if user is None or user.id != owner_user_id:
            return False
        checklist_message = event.checklist_message
        if checklist_message is None or checklist_message.chat is None:
            return False

        tracked = active_approval_checklists.get(
            int(checklist_message.chat.id),
            int(checklist_message.message_id),
        )
        if tracked is None:
            return False

        done_task_ids = {int(task_id) for task_id in (event.marked_as_done_task_ids or [])}
        if not done_task_ids:
            return False

        action: str | None = None
        if tracked.approve_task_id in done_task_ids:
            action = "approve"
        elif tracked.reject_task_id in done_task_ids:
            action = "reject"
        elif tracked.revise_task_id in done_task_ids:
            action = "revise"
        if action is None:
            return False

        active_approval_checklists.pop(tracked.chat_id, tracked.message_id)

        if action == "approve":
            try:
                job = await orchestrator.approve_job(tracked.job_id, user.id)
            except KeyError:
                await bot.send_message(chat_id=owner_user_id, text=f"Job {tracked.job_id} not found")
                return True
            await _close_approval_poll_for_job(tracked.job_id)
            if job.status != JobStatus.QUEUED:
                await bot.send_message(
                    chat_id=owner_user_id,
                    text=f"Job {tracked.job_id} was not awaiting approval (status={job.status})",
                )
                return True
            await bot.send_message(chat_id=owner_user_id, text=f"Approved job {tracked.job_id}; it is queued")
            return True

        try:
            job = await orchestrator.reject_job(tracked.job_id, user.id)
        except KeyError:
            await bot.send_message(chat_id=owner_user_id, text=f"Job {tracked.job_id} not found")
            return True
        await _close_approval_poll_for_job(tracked.job_id)
        if job.status != JobStatus.REJECTED:
            await bot.send_message(
                chat_id=owner_user_id,
                text=f"Job {tracked.job_id} was not awaiting approval (status={job.status})",
            )
            return True
        if action == "reject":
            await bot.send_message(chat_id=owner_user_id, text=f"Rejected job {tracked.job_id}; status={job.status}")
            return True
        await bot.send_message(
            chat_id=owner_user_id,
            text=f"Job {tracked.job_id} marked rejected. Send a revised prompt with /run when ready.",
        )
        return True

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
        if job.status in {JobStatus.CANCELED, JobStatus.REJECTED, JobStatus.QUEUED, JobStatus.RUNNING}:
            await _clear_approval_ui_for_job(job_id)
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
        chat_id = _chat_id(message)
        active_for_chat = orchestrator.get_active_session_for_chat(chat_id)
        payload = _args(message)
        parts = _split_args(payload)
        subcommand = parts[0].lower() if parts else "list"

        if subcommand == "list":
            sessions = session_manager.list_sessions()
            lines = [f"Active session for this chat: {active_for_chat or '(none)'}", "", "Sessions:"]
            if not sessions:
                lines.append("(none)")
                await message.answer("\n".join(lines))
                return
            for session in sessions:
                marker = " <- active" if active_for_chat and session.name == active_for_chat else ""
                lines.append(f"{session.name}: {session.status} pid={session.pid}{marker}")
            await message.answer("\n".join(lines))
            return

        if subcommand in {"clear", "reset"}:
            orchestrator.set_active_session_for_chat(chat_id, None)
            await message.answer("Active session cleared for this chat.")
            return

        if len(parts) < 2:
            await message.answer("Usage: /session <create|stop|use|clear|list> [name]")
            return
        name = _sanitize_session_token(parts[1])

        if subcommand == "create":
            try:
                result = await session_manager.create(name)
            except Exception as exc:
                await message.answer(f"Failed to create session: {exc}")
                return
            orchestrator.set_active_session_for_chat(chat_id, name)
            status = "created" if result.created else "already active"
            await message.answer(f"Session {name}: {status}\nActive session set to `{name}`.")
            return

        if subcommand == "stop":
            try:
                record = await session_manager.stop(name)
            except KeyError:
                await message.answer(f"Session {name} not found")
                return
            if active_for_chat == name:
                orchestrator.set_active_session_for_chat(chat_id, None)
            await message.answer(f"Session {name} stopped (status={record.status})")
            return

        if subcommand in {"use", "resume"}:
            if not session_manager.is_session_active(name):
                await message.answer(f"Session {name} is not active. Use /resume {name} or /session create {name}.")
                return
            orchestrator.set_active_session_for_chat(chat_id, name)
            await message.answer(f"Active session set to `{name}`.")
            return

        await message.answer("Unknown session subcommand. Use create|stop|use|clear|list")

    @router.message()
    async def attachment_run_handler(message: Message) -> None:
        if message.checklist_tasks_done is not None or message.checklist_tasks_added is not None:
            return
        if not _has_supported_attachments(message):
            return
        if not await guard.authorize(message):
            return
        prompt_input = _attachment_prompt_from_message(message)
        if prompt_input is None:
            await message.answer("Use a plain caption or `/run <prompt>` when sending attachments.")
            return
        workdir = orchestrator.get_effective_workdir()
        try:
            attachments = await _download_message_attachments(message=message, bot=bot, workdir=workdir)
        except Exception as exc:
            await message.answer(f"Failed to download attachment: {exc}")
            return
        if not attachments:
            await message.answer("No supported attachment found. Send a file or image.")
            return

        prompt = _build_attachment_prompt(prompt_input, attachments)
        chat_id = _chat_id(message)
        agent_hint = chat_agents.get(chat_id)
        effective_prompt = f"Use agent profile `{agent_hint}` for this response.\n\n{prompt}" if agent_hint else prompt
        active_session = orchestrator.get_active_session_for_chat(chat_id)
        if active_session:
            if not session_manager.is_session_active(active_session):
                await message.answer(
                    f"Active session `{active_session}` is inactive. Use /resume {active_session} or /session clear."
                )
                return
            job = await orchestrator.submit_job(
                prompt=effective_prompt,
                mode=JobMode.SESSION,
                session_name=active_session,
            )
            await message.answer(
                f"Queued session attachment job {job.id} in `{active_session}` ({job.status}) "
                f"with {len(attachments)} attachment(s)."
            )
            return

        job = await orchestrator.submit_job(prompt=effective_prompt, mode=JobMode.EPHEMERAL)
        await message.answer(f"Queued attachment job {job.id} with status {job.status} ({len(attachments)} file(s)).")

    @router.message()
    async def fallback_handler(message: Message) -> None:
        if not await guard.authorize(message):
            return
        if message.checklist_tasks_done is not None:
            await _handle_checklist_approval_message(message)
            return
        if message.checklist_tasks_added is not None:
            return
        await message.answer("Unknown command. Use /start for help.")

    dispatcher.include_router(router)
    return dispatcher
