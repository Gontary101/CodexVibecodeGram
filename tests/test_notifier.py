from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_telegram.approval_checklists import ApprovalChecklistStore
from codex_telegram.approval_polls import ApprovalPollStore
from codex_telegram.assistant_polls import AssistantPollStore
from codex_telegram.models import Artifact, Job, JobMode, JobStatus, RiskLevel
from codex_telegram.notifier import TelegramNotifier, _extract_poll_candidate


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.polls: list[tuple[int, str, list[str], bool, bool]] = []
        self.checklists: list[tuple[str, int, str, list[tuple[int, str]]]] = []
        self.documents: list[tuple[int, str, str | None]] = []

    async def send_message(self, chat_id: int, text: str):  # type: ignore[no-untyped-def]
        self.messages.append((chat_id, text))

    async def send_poll(  # type: ignore[no-untyped-def]
        self,
        chat_id: int,
        question: str,
        options: list[str],
        is_anonymous: bool,
        allows_multiple_answers: bool,
    ):
        self.polls.append((chat_id, question, options, is_anonymous, allows_multiple_answers))
        return SimpleNamespace(message_id=77, poll=SimpleNamespace(id="poll-77"))

    async def send_checklist(  # type: ignore[no-untyped-def]
        self,
        business_connection_id: str,
        chat_id: int,
        checklist,
    ):
        tasks = [(int(task.id), str(task.text)) for task in checklist.tasks]
        self.checklists.append((business_connection_id, chat_id, str(checklist.title), tasks))
        return SimpleNamespace(message_id=88)

    async def send_document(self, chat_id: int, document, caption: str | None = None):  # type: ignore[no-untyped-def]
        self.documents.append((chat_id, str(document.path), caption))


class FailingChecklistBot(FakeBot):
    async def send_checklist(  # type: ignore[no-untyped-def]
        self,
        business_connection_id: str,
        chat_id: int,
        checklist,
    ):
        raise RuntimeError("checklist unavailable")


def _job(status: JobStatus, summary: str | None = None, error: str | None = None) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=7,
        status=status,
        mode=JobMode.EPHEMERAL,
        prompt="prompt",
        created_at=now,
        updated_at=now,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
        summary_text=summary,
        error_text=error,
    )


@pytest.mark.asyncio
async def test_success_notification_is_plain_summary() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot=bot, owner_chat_id=123)

    await notifier.send_job_status(_job(JobStatus.SUCCEEDED, summary="Hello from Codex"), "Job completed")

    assert bot.messages == [(123, "Hello from Codex")]


@pytest.mark.asyncio
async def test_success_notification_compact_adds_job_footer() -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot=bot, owner_chat_id=123, response_mode="compact")

    await notifier.send_job_status(_job(JobStatus.SUCCEEDED, summary="Hello from Codex"), "Job completed")

    assert bot.messages == [(123, "Hello from Codex\n\n(job 7)")]


@pytest.mark.asyncio
async def test_send_approval_request_emits_poll_and_registers_job() -> None:
    bot = FakeBot()
    poll_store = ApprovalPollStore()
    notifier = TelegramNotifier(bot=bot, owner_chat_id=123, approval_polls=poll_store)

    await notifier.send_approval_request(_job(JobStatus.AWAITING_APPROVAL), "matches medium-risk pattern")

    assert bot.messages[0][1].startswith("Job 7 requires approval.")
    assert len(bot.polls) == 1
    poll = poll_store.get("poll-77")
    assert poll is not None
    assert poll.job_id == 7


@pytest.mark.asyncio
async def test_send_approval_request_emits_checklist_when_business_mode_enabled() -> None:
    bot = FakeBot()
    checklist_store = ApprovalChecklistStore()
    poll_store = ApprovalPollStore()
    notifier = TelegramNotifier(
        bot=bot,
        owner_chat_id=123,
        approval_polls=poll_store,
        approval_checklists=checklist_store,
        business_connection_id="bc-1",
    )

    await notifier.send_approval_request(_job(JobStatus.AWAITING_APPROVAL), "matches medium-risk pattern")

    assert bot.checklists
    assert bot.polls == []
    checklist = checklist_store.get(123, 88)
    assert checklist is not None
    assert checklist.job_id == 7


@pytest.mark.asyncio
async def test_send_approval_request_falls_back_to_poll_when_checklist_fails() -> None:
    bot = FailingChecklistBot()
    checklist_store = ApprovalChecklistStore()
    poll_store = ApprovalPollStore()
    notifier = TelegramNotifier(
        bot=bot,
        owner_chat_id=123,
        approval_polls=poll_store,
        approval_checklists=checklist_store,
        business_connection_id="bc-1",
    )

    await notifier.send_approval_request(_job(JobStatus.AWAITING_APPROVAL), "matches medium-risk pattern")

    assert bot.polls
    poll = poll_store.get("poll-77")
    assert poll is not None
    assert checklist_store.get(123, 88) is None


def test_extract_poll_candidate_from_multiple_choice_summary() -> None:
    candidate = _extract_poll_candidate(
        "Which direction should I take next?\n"
        "1. Implement the API first\n"
        "2. Write tests first\n"
        "3. Refactor existing code first"
    )

    assert candidate is not None
    assert candidate.question == "Which direction should I take next?"
    assert candidate.options == (
        "Implement the API first",
        "Write tests first",
        "Refactor existing code first",
    )


def test_extract_poll_candidate_from_explicit_poll_block() -> None:
    candidate = _extract_poll_candidate(
        "Plan is ready.\n"
        "[poll]\n"
        "Question: Which deployment mode should I run?\n"
        "- Canary\n"
        "- Blue/Green\n"
        "[/poll]"
    )

    assert candidate is not None
    assert candidate.question == "Which deployment mode should I run?"
    assert candidate.options == ("Canary", "Blue/Green")


@pytest.mark.asyncio
async def test_success_notification_creates_follow_up_poll_for_multiple_choice_summary() -> None:
    bot = FakeBot()
    assistant_polls = AssistantPollStore()
    notifier = TelegramNotifier(bot=bot, owner_chat_id=123, assistant_polls=assistant_polls)

    await notifier.send_job_status(
        _job(
            JobStatus.SUCCEEDED,
            summary=(
                "Which implementation should I execute?\n"
                "- Keep the current parser\n"
                "- Replace it with a strict parser"
            ),
        ),
        "Job completed",
    )

    assert bot.messages
    assert len(bot.polls) == 1
    poll = assistant_polls.get("poll-77")
    assert poll is not None
    assert poll.source_job_id == 7
    assert poll.options == ("Keep the current parser", "Replace it with a strict parser")


@pytest.mark.asyncio
async def test_send_artifacts_skips_log_kind(tmp_path: Path) -> None:
    bot = FakeBot()
    notifier = TelegramNotifier(bot=bot, owner_chat_id=123)

    log_path = tmp_path / "stderr.log"
    img_path = tmp_path / "preview.png"
    log_path.write_text("error\n", encoding="utf-8")
    img_path.write_bytes(b"PNG")

    artifacts = [
        Artifact(id=1, job_id=10, kind="log", path=log_path, size_bytes=log_path.stat().st_size, sha256="a"),
        Artifact(id=2, job_id=10, kind="image", path=img_path, size_bytes=img_path.stat().st_size, sha256="b"),
    ]

    await notifier.send_artifacts(artifacts)

    assert len(bot.documents) == 1
    assert bot.documents[0][1].endswith("preview.png")
