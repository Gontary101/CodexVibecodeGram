from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_telegram.models import Artifact, Job, JobMode, JobStatus, RiskLevel
from codex_telegram.notifier import TelegramNotifier


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.documents: list[tuple[int, str, str | None]] = []

    async def send_message(self, chat_id: int, text: str):  # type: ignore[no-untyped-def]
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id: int, document, caption: str | None = None):  # type: ignore[no-untyped-def]
        self.documents.append((chat_id, str(document.path), caption))


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
