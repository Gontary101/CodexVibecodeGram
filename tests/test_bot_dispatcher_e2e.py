from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import Update

from codex_telegram.bot import build_dispatcher
from codex_telegram.models import JobMode, SessionRecord, SessionStatus
from codex_telegram.sessions import SessionCreateResult


@dataclass(slots=True)
class _SessionState:
    name: str
    active: bool


class FakeOrchestrator:
    def __init__(self) -> None:
        self._chat_active: dict[int, str | None] = {}
        self.submitted: list[tuple[str, JobMode, str | None]] = []
        self._job_counter = 0

    async def submit_job(self, prompt: str, mode: JobMode, session_name: str | None = None):
        self._job_counter += 1
        self.submitted.append((prompt, mode, session_name))
        return SimpleNamespace(id=self._job_counter, status="queued")

    def get_active_session_for_chat(self, chat_id: int) -> str | None:
        return self._chat_active.get(chat_id)

    def set_active_session_for_chat(self, chat_id: int, session_name: str | None) -> None:
        self._chat_active[chat_id] = session_name

    # Unused in these tests, required by dispatcher for other handlers.
    def get_runtime_profile(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            model=None,
            reasoning_effort=None,
            sandbox_mode=None,
            approval_policy=None,
            web_search=None,
            personality="none",
            personality_instruction="",
            experimental_features=set(),
        )

    def get_allowed_workdirs(self):  # type: ignore[no-untyped-def]
        return (Path("."),)

    def get_effective_workdir(self):  # type: ignore[no-untyped-def]
        return Path(".")

    def get_effective_approval_policy(self) -> str:
        return "on-request"

    def count_jobs_by_status(self):  # type: ignore[no-untyped-def]
        return {}

    def running_jobs_count(self) -> int:
        return 0

    def list_jobs(self, limit: int = 20):  # type: ignore[no-untyped-def]
        return []


class FakeSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}

    def is_session_active(self, session_name: str) -> bool:
        state = self._sessions.get(session_name)
        return bool(state and state.active)

    async def create(self, session_name: str) -> SessionCreateResult:
        existing = self._sessions.get(session_name)
        if existing and existing.active:
            created = False
        else:
            self._sessions[session_name] = _SessionState(name=session_name, active=True)
            created = True
        now = datetime.now(UTC)
        record = SessionRecord(
            name=session_name,
            status=SessionStatus.ACTIVE,
            pid=None,
            started_at=now,
            last_seen_at=now,
            metadata_json=None,
        )
        return SessionCreateResult(record=record, created=created)

    async def stop(self, session_name: str) -> SessionRecord:
        if session_name not in self._sessions:
            raise KeyError(session_name)
        self._sessions[session_name].active = False
        now = datetime.now(UTC)
        return SessionRecord(
            name=session_name,
            status=SessionStatus.INACTIVE,
            pid=None,
            started_at=now,
            last_seen_at=now,
            metadata_json=None,
        )

    def list_sessions(self) -> list[SessionRecord]:
        now = datetime.now(UTC)
        out: list[SessionRecord] = []
        for name, state in sorted(self._sessions.items(), key=lambda x: x[0]):
            out.append(
                SessionRecord(
                    name=name,
                    status=SessionStatus.ACTIVE if state.active else SessionStatus.INACTIVE,
                    pid=None,
                    started_at=now,
                    last_seen_at=now,
                    metadata_json=None,
                )
            )
        return out


class FakeVideoService:
    async def generate_for_job(self, job_id: int):  # type: ignore[no-untyped-def]
        raise KeyError(job_id)


def _make_update(text: str, *, user_id: int, chat_id: int, update_id: int = 1) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(tz=UTC).timestamp()),
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "tester"},
                "text": text,
            },
        }
    )


@pytest.mark.asyncio
async def test_dispatcher_new_then_run_uses_session_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent_texts: list[str] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        text = getattr(method, "text", None)
        if text is not None:
            sent_texts.append(str(text))
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
    )

    await dispatcher.feed_update(bot, _make_update("/new alpha", user_id=42, chat_id=42, update_id=1))
    await dispatcher.feed_update(bot, _make_update("/run hello", user_id=42, chat_id=42, update_id=2))

    assert any("Active session set to `alpha`." in text for text in sent_texts)
    assert orchestrator.submitted
    _, mode, session_name = orchestrator.submitted[-1]
    assert mode == JobMode.SESSION
    assert session_name == "alpha"


@pytest.mark.asyncio
async def test_dispatcher_run_without_session_uses_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
    )

    await dispatcher.feed_update(bot, _make_update("/run hi", user_id=42, chat_id=42, update_id=1))

    assert orchestrator.submitted
    _, mode, session_name = orchestrator.submitted[-1]
    assert mode == JobMode.EPHEMERAL
    assert session_name is None


@pytest.mark.asyncio
async def test_dispatcher_rejects_unauthorized_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent_texts: list[str] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        text = getattr(method, "text", None)
        if text is not None:
            sent_texts.append(str(text))
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
    )

    await dispatcher.feed_update(bot, _make_update("/run blocked", user_id=7, chat_id=7, update_id=1))

    assert sent_texts == ["Unauthorized"]
    assert orchestrator.submitted == []


@pytest.mark.asyncio
async def test_dispatcher_session_clear_unsets_chat_pointer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
    )

    await dispatcher.feed_update(bot, _make_update("/new alpha", user_id=42, chat_id=42, update_id=1))
    assert orchestrator.get_active_session_for_chat(42) == "alpha"

    await dispatcher.feed_update(bot, _make_update("/session clear", user_id=42, chat_id=42, update_id=2))
    assert orchestrator.get_active_session_for_chat(42) is None
