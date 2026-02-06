from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from .models import Artifact, Job, JobStatus

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        owner_chat_id: int,
        max_chunk: int = 3500,
        response_mode: str = "natural",
    ) -> None:
        self._bot = bot
        self._owner_chat_id = owner_chat_id
        self._max_chunk = max_chunk
        self._response_mode = response_mode

    async def send_text(self, text: str) -> None:
        chunks = [text[i : i + self._max_chunk] for i in range(0, len(text), self._max_chunk)]
        if not chunks:
            chunks = ["(empty message)"]
        for chunk in chunks:
            await self._bot.send_message(self._owner_chat_id, chunk)

    async def send_job_status(self, job: Job, heading: str) -> None:
        if job.status == JobStatus.SUCCEEDED:
            natural = (job.summary_text or "").strip()
            if self._response_mode == "verbose":
                msg = f"{heading}\njob={job.id}\nstatus={job.status}\n\n{natural}" if natural else f"{heading}\njob={job.id}"
                await self.send_text(msg)
            elif self._response_mode == "compact":
                base = natural if natural else "Completed."
                await self.send_text(f"{base}\n\n(job {job.id})")
            else:
                await self.send_text(natural if natural else f"Job {job.id} completed.")
            return

        if job.status == JobStatus.FAILED:
            first_error_line = (job.error_text or job.summary_text or "").strip().splitlines()
            if first_error_line:
                await self.send_text(f"Job {job.id} failed: {first_error_line[0][:800]}\nUse /info {job.id} for details.")
            else:
                await self.send_text(f"Job {job.id} failed. Use /info {job.id} for details.")
            return

        if job.status == JobStatus.REJECTED:
            await self.send_text(f"Job {job.id} was rejected.")
            return

        if job.status == JobStatus.CANCELED:
            await self.send_text(f"Job {job.id} was canceled.")
            return

        await self.send_text(f"{heading}\njob={job.id}\nstatus={job.status}")

    async def send_artifacts(self, artifacts: list[Artifact], max_files: int = 5) -> None:
        sent = 0
        for artifact in artifacts:
            if sent >= max_files:
                break
            if artifact.kind == "log":
                continue
            path = Path(artifact.path)
            if not path.exists():
                continue
            if path.stat().st_size == 0:
                continue
            try:
                await self._bot.send_document(
                    self._owner_chat_id,
                    document=FSInputFile(path),
                    caption=f"job={artifact.job_id} kind={artifact.kind} file={path.name}",
                )
                sent += 1
            except Exception:
                logger.exception("failed sending artifact", extra={"path": str(path)})
