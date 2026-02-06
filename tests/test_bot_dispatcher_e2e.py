from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import Update

from codex_telegram.approval_checklists import APPROVAL_TASK_APPROVE, ApprovalChecklist, ApprovalChecklistStore
from codex_telegram.approval_polls import ApprovalPoll, ApprovalPollStore
from codex_telegram.assistant_polls import AssistantPoll, AssistantPollStore
from codex_telegram.bot import build_dispatcher
from codex_telegram.feature_polls import FEATURE_ROADMAP_POLLS
from codex_telegram.models import JobMode, JobStatus, SessionRecord, SessionStatus
from codex_telegram.sessions import SessionCreateResult


@dataclass(slots=True)
class _SessionState:
    name: str
    active: bool


class FakeOrchestrator:
    def __init__(self, workdir: Path | None = None) -> None:
        self._chat_active: dict[int, str | None] = {}
        self.submitted: list[tuple[str, JobMode, str | None]] = []
        self._job_counter = 0
        self._job_status: dict[int, JobStatus] = {}
        self.approved_jobs: list[tuple[int, int]] = []
        self.rejected_jobs: list[tuple[int, int]] = []
        self._workdir = (workdir or Path(".")).resolve()

    async def submit_job(self, prompt: str, mode: JobMode, session_name: str | None = None):
        self._job_counter += 1
        self.submitted.append((prompt, mode, session_name))
        self._job_status[self._job_counter] = JobStatus.QUEUED
        return SimpleNamespace(id=self._job_counter, status="queued")

    def seed_awaiting_job(self, job_id: int) -> None:
        self._job_status[job_id] = JobStatus.AWAITING_APPROVAL

    async def approve_job(self, job_id: int, user_id: int):  # type: ignore[no-untyped-def]
        current = self._job_status.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if current == JobStatus.AWAITING_APPROVAL:
            self._job_status[job_id] = JobStatus.QUEUED
        self.approved_jobs.append((job_id, user_id))
        return SimpleNamespace(id=job_id, status=self._job_status[job_id])

    async def reject_job(self, job_id: int, user_id: int):  # type: ignore[no-untyped-def]
        current = self._job_status.get(job_id)
        if current is None:
            raise KeyError(job_id)
        if current == JobStatus.AWAITING_APPROVAL:
            self._job_status[job_id] = JobStatus.REJECTED
        self.rejected_jobs.append((job_id, user_id))
        return SimpleNamespace(id=job_id, status=self._job_status[job_id])

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
        return self._workdir

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


def _make_poll_answer_update(
    poll_id: str,
    option_id: int,
    *,
    user_id: int,
    update_id: int = 1,
) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "poll_answer": {
                "poll_id": poll_id,
                "option_ids": [option_id],
                "user": {"id": user_id, "is_bot": False, "first_name": "owner"},
            },
        }
    )


def _make_poll_answer_update_with_options(
    poll_id: str,
    option_ids: list[int],
    *,
    user_id: int,
    update_id: int = 1,
) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "poll_answer": {
                "poll_id": poll_id,
                "option_ids": option_ids,
                "user": {"id": user_id, "is_bot": False, "first_name": "owner"},
            },
        }
    )


def _make_checklist_done_update(
    *,
    user_id: int,
    chat_id: int,
    update_id: int,
    checklist_message_id: int,
    task_ids: list[int],
) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": int(datetime.now(tz=UTC).timestamp()),
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "owner"},
                "checklist_tasks_done": {
                    "checklist_message": {
                        "message_id": checklist_message_id,
                        "date": int(datetime.now(tz=UTC).timestamp()),
                        "chat": {"id": chat_id, "type": "private"},
                    },
                    "marked_as_done_task_ids": task_ids,
                    "marked_as_not_done_task_ids": [],
                },
            },
        }
    )


def _make_document_update(
    *,
    user_id: int,
    chat_id: int,
    update_id: int,
    caption: str | None = None,
) -> Update:
    payload: dict[str, object] = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(tz=UTC).timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "tester"},
            "document": {
                "file_id": f"doc-{update_id}",
                "file_unique_id": f"uniq-{update_id}",
                "file_name": "spec.txt",
                "mime_type": "text/plain",
                "file_size": 12,
            },
        },
    }
    if caption is not None:
        payload["message"]["caption"] = caption  # type: ignore[index]
    return Update.model_validate(payload)


def _make_photo_update(
    *,
    user_id: int,
    chat_id: int,
    update_id: int,
    caption: str | None = None,
) -> Update:
    payload: dict[str, object] = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(tz=UTC).timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "tester"},
            "photo": [
                {
                    "file_id": f"photo-{update_id}",
                    "file_unique_id": f"photo-uniq-{update_id}",
                    "width": 320,
                    "height": 200,
                    "file_size": 2048,
                }
            ],
        },
    }
    if caption is not None:
        payload["message"]["caption"] = caption  # type: ignore[index]
    return Update.model_validate(payload)


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
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    orchestrator = FakeOrchestrator(workdir=workdir)
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


@pytest.mark.asyncio
async def test_dispatcher_poll_answer_approves_waiting_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []
    stopped_polls: list[tuple[int, int]] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        method_name = method.__class__.__name__
        if method_name == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
        if method_name == "StopPoll":
            stopped_polls.append((int(method.chat_id), int(method.message_id)))
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    orchestrator.seed_awaiting_job(10)
    sessions = FakeSessionManager()
    approval_polls = ApprovalPollStore()
    approval_polls.register(ApprovalPoll(poll_id="poll-10", job_id=10, chat_id=42, message_id=700))
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
        approval_polls=approval_polls,
    )

    await dispatcher.feed_update(
        bot,
        _make_poll_answer_update("poll-10", 0, user_id=42, update_id=1),
    )

    assert orchestrator.approved_jobs == [(10, 42)]
    assert stopped_polls == [(42, 700)]
    assert any("Approved job 10" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_dispatcher_unknown_command_returns_fallback_help(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        if method.__class__.__name__ == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
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

    await dispatcher.feed_update(bot, _make_update("/unknown", user_id=42, chat_id=42, update_id=1))

    assert sent_texts == ["Unknown command. Use /start for help."]


@pytest.mark.asyncio
async def test_dispatcher_checklist_done_approves_even_when_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        if method.__class__.__name__ == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    orchestrator.seed_awaiting_job(10)
    sessions = FakeSessionManager()
    approval_checklists = ApprovalChecklistStore()
    approval_checklists.register(ApprovalChecklist(job_id=10, chat_id=42, message_id=900))
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=60.0,
        runs_dir=tmp_path / "runs",
        approval_checklists=approval_checklists,
    )

    await dispatcher.feed_update(bot, _make_update("/status", user_id=42, chat_id=42, update_id=1))
    await dispatcher.feed_update(
        bot,
        _make_checklist_done_update(
            user_id=42,
            chat_id=42,
            update_id=2,
            checklist_message_id=900,
            task_ids=[APPROVAL_TASK_APPROVE],
        ),
    )

    assert orchestrator.approved_jobs == [(10, 42)]
    assert any("Approved job 10" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_dispatcher_model_show_uses_codex_config_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.3-codex"\nmodel_reasoning_effort = "xhigh"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_PROFILE", raising=False)

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        if method.__class__.__name__ == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
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

    await dispatcher.feed_update(bot, _make_update("/model", user_id=42, chat_id=42, update_id=1))

    assert sent_texts
    assert "model=gpt-5.3-codex" in sent_texts[-1]
    assert "reasoning_effort=xhigh" in sent_texts[-1]


@pytest.mark.asyncio
async def test_dispatcher_poll_command_sends_and_registers_test_poll(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []
    sent_polls: list[tuple[str, list[str]]] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        name = method.__class__.__name__
        if name == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
            return None
        if name == "SendPoll":
            sent_polls.append((str(method.question), [str(option) for option in method.options]))
            return SimpleNamespace(message_id=333, poll=SimpleNamespace(id="manual-poll-333"))
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    assistant_polls = AssistantPollStore()
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
        assistant_polls=assistant_polls,
    )

    await dispatcher.feed_update(bot, _make_update("/poll", user_id=42, chat_id=42, update_id=1))

    assert sent_polls == [
        (
            "Poll feature smoke test: does this new poll flow work?",
            ["Yes, works", "Partially works", "No, needs fixes"],
        )
    ]
    tracked = assistant_polls.get("manual-poll-333")
    assert tracked is not None
    assert tracked.source_job_id is None
    assert tracked.chat_id == 42
    assert tracked.message_id == 333
    assert tracked.options == ("Yes, works", "Partially works", "No, needs fixes")
    assert any("Test poll created." in text for text in sent_texts)


@pytest.mark.asyncio
async def test_dispatcher_featurepolls_command_sends_curated_polls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []
    sent_polls: list[tuple[str, list[str]]] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        name = method.__class__.__name__
        if name == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
            return None
        if name == "SendPoll":
            poll_index = len(sent_polls) + 1
            sent_polls.append((str(method.question), [str(option) for option in method.options]))
            return SimpleNamespace(message_id=400 + poll_index, poll=SimpleNamespace(id=f"feature-poll-{poll_index}"))
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    assistant_polls = AssistantPollStore()
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
        assistant_polls=assistant_polls,
    )

    await dispatcher.feed_update(bot, _make_update("/featurepolls", user_id=42, chat_id=42, update_id=1))

    expected_count = len(FEATURE_ROADMAP_POLLS)
    assert sent_polls == [(template.question, list(template.options)) for template in FEATURE_ROADMAP_POLLS]
    assert len(sent_polls) == expected_count
    for idx, template in enumerate(FEATURE_ROADMAP_POLLS, start=1):
        tracked = assistant_polls.get(f"feature-poll-{idx}")
        assert tracked is not None
        assert tracked.source_job_id is None
        assert tracked.chat_id == 42
        assert tracked.message_id == 400 + idx
        assert tracked.question == template.question
        assert tracked.options == template.options
        assert tracked.allows_multiple_answers == template.allows_multiple_answers
    assert any(f"Created {expected_count} feature poll(s)." in text for text in sent_texts)


@pytest.mark.asyncio
async def test_dispatcher_attachment_message_downloads_and_submits_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        if method.__class__.__name__ == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
        return None

    async def _fake_download(  # type: ignore[no-untyped-def]
        self,
        file,
        destination=None,
        timeout=30,
        chunk_size=65536,
        seek=True,
    ):
        assert destination is not None
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("attachment payload", encoding="utf-8")
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)
    monkeypatch.setattr(Bot, "download", _fake_download)

    bot = Bot("12345:token")
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    orchestrator = FakeOrchestrator(workdir=workdir)
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

    await dispatcher.feed_update(
        bot,
        _make_document_update(user_id=42, chat_id=42, update_id=1, caption="Summarize this file"),
    )

    assert orchestrator.submitted
    prompt, mode, session_name = orchestrator.submitted[-1]
    assert mode == JobMode.EPHEMERAL
    assert session_name is None
    assert "Telegram attachments saved in workspace" in prompt
    assert "User request:\nSummarize this file" in prompt
    assert "spec.txt" in prompt
    assert any("Queued attachment job" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_dispatcher_attachment_without_caption_uses_default_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return None

    async def _fake_download(  # type: ignore[no-untyped-def]
        self,
        file,
        destination=None,
        timeout=30,
        chunk_size=65536,
        seek=True,
    ):
        assert destination is not None
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("attachment payload", encoding="utf-8")
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)
    monkeypatch.setattr(Bot, "download", _fake_download)

    bot = Bot("12345:token")
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    orchestrator = FakeOrchestrator(workdir=workdir)
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

    await dispatcher.feed_update(bot, _make_document_update(user_id=42, chat_id=42, update_id=1))

    assert orchestrator.submitted
    prompt, _, _ = orchestrator.submitted[-1]
    assert "No extra user prompt was provided." in prompt


@pytest.mark.asyncio
async def test_dispatcher_photo_attachment_submits_image_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return None

    async def _fake_download(  # type: ignore[no-untyped-def]
        self,
        file,
        destination=None,
        timeout=30,
        chunk_size=65536,
        seek=True,
    ):
        assert destination is not None
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"JPEGDATA")
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)
    monkeypatch.setattr(Bot, "download", _fake_download)

    bot = Bot("12345:token")
    workdir = tmp_path / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    orchestrator = FakeOrchestrator(workdir=workdir)
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

    await dispatcher.feed_update(
        bot,
        _make_photo_update(user_id=42, chat_id=42, update_id=1, caption="Describe this image"),
    )

    assert orchestrator.submitted
    prompt, _, _ = orchestrator.submitted[-1]
    assert "- image:" in prompt
    assert "Describe this image" in prompt


@pytest.mark.asyncio
async def test_dispatcher_assistant_poll_answer_queues_follow_up_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_texts: list[str] = []
    stopped_polls: list[tuple[int, int]] = []

    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        name = method.__class__.__name__
        if name == "SendMessage":
            sent_texts.append(str(getattr(method, "text", "")))
        if name == "StopPoll":
            stopped_polls.append((int(method.chat_id), int(method.message_id)))
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    assistant_polls = AssistantPollStore()
    assistant_polls.register(
        AssistantPoll(
            poll_id="assistant-poll-1",
            source_job_id=9,
            chat_id=42,
            message_id=500,
            question="Which option should I execute?",
            options=("A", "B", "C"),
            allows_multiple_answers=False,
        )
    )
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
        assistant_polls=assistant_polls,
    )

    await dispatcher.feed_update(
        bot,
        _make_poll_answer_update("assistant-poll-1", 1, user_id=42, update_id=1),
    )

    assert stopped_polls == [(42, 500)]
    assert orchestrator.submitted
    prompt, mode, _ = orchestrator.submitted[-1]
    assert mode == JobMode.EPHEMERAL
    assert "Selected option(s): B" in prompt
    assert any("Queued follow-up job" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_dispatcher_manual_assistant_poll_answer_omits_fake_job_reference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    assistant_polls = AssistantPollStore()
    assistant_polls.register(
        AssistantPoll(
            poll_id="assistant-poll-manual",
            source_job_id=None,
            chat_id=42,
            message_id=501,
            question="Does this poll flow work?",
            options=("Yes", "No"),
            allows_multiple_answers=False,
        )
    )
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
        assistant_polls=assistant_polls,
    )

    await dispatcher.feed_update(
        bot,
        _make_poll_answer_update("assistant-poll-manual", 0, user_id=42, update_id=1),
    )

    assert orchestrator.submitted
    prompt, mode, _ = orchestrator.submitted[-1]
    assert mode == JobMode.EPHEMERAL
    assert prompt.startswith("The user answered your poll.\nQuestion: Does this poll flow work?")
    assert "for job" not in prompt


@pytest.mark.asyncio
async def test_dispatcher_assistant_poll_empty_answer_keeps_poll_tracking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _fake_call(self, method, request_timeout=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(Bot, "__call__", _fake_call)

    bot = Bot("12345:token")
    orchestrator = FakeOrchestrator()
    sessions = FakeSessionManager()
    assistant_polls = AssistantPollStore()
    assistant_polls.register(
        AssistantPoll(
            poll_id="assistant-poll-empty",
            source_job_id=12,
            chat_id=42,
            message_id=501,
            question="Pick one",
            options=("A", "B"),
            allows_multiple_answers=False,
        )
    )
    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        session_manager=sessions,  # type: ignore[arg-type]
        video_service=FakeVideoService(),  # type: ignore[arg-type]
        owner_user_id=42,
        command_cooldown_seconds=0.0,
        runs_dir=tmp_path / "runs",
        assistant_polls=assistant_polls,
    )

    await dispatcher.feed_update(
        bot,
        _make_poll_answer_update_with_options("assistant-poll-empty", [], user_id=42, update_id=1),
    )
    assert assistant_polls.get("assistant-poll-empty") is not None

    await dispatcher.feed_update(
        bot,
        _make_poll_answer_update("assistant-poll-empty", 1, user_id=42, update_id=2),
    )
    assert assistant_polls.get("assistant-poll-empty") is None
    assert orchestrator.submitted
    prompt, _, _ = orchestrator.submitted[-1]
    assert "Selected option(s): B" in prompt
