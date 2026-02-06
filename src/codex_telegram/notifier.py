from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InputChecklist, InputChecklistTask

from .approval_checklists import (
    APPROVAL_CHECKLIST_TASKS,
    APPROVAL_TASK_APPROVE,
    APPROVAL_TASK_REJECT,
    APPROVAL_TASK_REVISE,
    ApprovalChecklist,
    ApprovalChecklistStore,
)
from .approval_polls import ApprovalPoll, ApprovalPollStore, APPROVAL_POLL_OPTIONS
from .assistant_polls import AssistantPoll, AssistantPollStore
from .models import Artifact, Job, JobStatus

logger = logging.getLogger(__name__)

_OPTION_PATTERN = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+|[A-Za-z][.)]\s+)(.+)$")
_QUESTION_KEYWORDS = ("which", "choose", "select", "pick", "option", "prefer", "vote", "should", "what should")
_POLL_BLOCK_PATTERN = re.compile(r"\[poll\](.*?)\[/poll\]", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class PollCandidate:
    question: str
    options: tuple[str, ...]
    allows_multiple_answers: bool = False


def _extract_poll_candidate(text: str) -> PollCandidate | None:
    block_match = _POLL_BLOCK_PATTERN.search(text)
    if block_match is not None:
        block_lines = [line.strip() for line in block_match.group(1).splitlines() if line.strip()]
        question = ""
        options: list[str] = []
        for line in block_lines:
            lowered = line.lower()
            if lowered.startswith("question:"):
                question = line.split(":", 1)[1].strip()
                continue
            matched = _OPTION_PATTERN.match(line)
            if matched is not None:
                option = matched.group(1).strip().strip("`")
                if option:
                    options.append(option)
                continue
            if not question and line.endswith("?"):
                question = line
        deduped_block_options = list(dict.fromkeys(options))
        if question and len(deduped_block_options) >= 2:
            return PollCandidate(
                question=question[:300],
                options=tuple(option[:100] for option in deduped_block_options[:10]),
                allows_multiple_answers=False,
            )

    lines = [line.strip() for line in text.splitlines()]
    for idx, line in enumerate(lines):
        if not line or not line.endswith("?"):
            continue
        lowered = line.lower()
        if not any(keyword in lowered for keyword in _QUESTION_KEYWORDS):
            continue
        options: list[str] = []
        cursor = idx + 1
        while cursor < len(lines):
            current = lines[cursor]
            if not current:
                if options:
                    break
                cursor += 1
                continue
            matched = _OPTION_PATTERN.match(current)
            if matched is None:
                if options:
                    break
                cursor += 1
                continue
            option = matched.group(1).strip().strip("`")
            if option:
                options.append(option)
            cursor += 1
        deduped = list(dict.fromkeys(options))
        if len(deduped) < 2:
            continue
        return PollCandidate(
            question=line[:300],
            options=tuple(opt[:100] for opt in deduped[:10]),
            allows_multiple_answers=False,
        )
    return None


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        owner_chat_id: int,
        max_chunk: int = 3500,
        response_mode: str = "natural",
        approval_polls: ApprovalPollStore | None = None,
        approval_checklists: ApprovalChecklistStore | None = None,
        assistant_polls: AssistantPollStore | None = None,
        business_connection_id: str | None = None,
    ) -> None:
        self._bot = bot
        self._owner_chat_id = owner_chat_id
        self._max_chunk = max_chunk
        self._response_mode = response_mode
        self._approval_polls = approval_polls
        self._approval_checklists = approval_checklists
        self._assistant_polls = assistant_polls
        self._business_connection_id = business_connection_id.strip() if business_connection_id else None

    async def send_text(self, text: str) -> None:
        chunks = [text[i : i + self._max_chunk] for i in range(0, len(text), self._max_chunk)]
        if not chunks:
            chunks = ["(empty message)"]
        for chunk in chunks:
            await self._bot.send_message(self._owner_chat_id, chunk)

    async def send_approval_request(self, job: Job, reason: str) -> None:
        detail = f"Job {job.id} requires approval.\nreason={reason}"
        if self._approval_checklists is None and self._approval_polls is None:
            await self.send_text(f"{detail}\nUse /approve {job.id} or /reject {job.id}.")
            return

        await self.send_text(detail)
        if await self._send_approval_checklist(job):
            return
        if await self._send_approval_poll(job):
            return
        await self.send_text(f"Use /approve {job.id} or /reject {job.id}.")

    async def _send_approval_checklist(self, job: Job) -> bool:
        if self._approval_checklists is None or not self._business_connection_id:
            return False
        try:
            checklist = InputChecklist(
                title=f"Approval for job {job.id}",
                tasks=[InputChecklistTask(id=task_id, text=label) for task_id, label in APPROVAL_CHECKLIST_TASKS],
            )
            sent = await self._bot.send_checklist(
                business_connection_id=self._business_connection_id,
                chat_id=self._owner_chat_id,
                checklist=checklist,
            )
            self._approval_checklists.register(
                ApprovalChecklist(
                    job_id=job.id,
                    chat_id=self._owner_chat_id,
                    message_id=sent.message_id,
                    approve_task_id=APPROVAL_TASK_APPROVE,
                    reject_task_id=APPROVAL_TASK_REJECT,
                    revise_task_id=APPROVAL_TASK_REVISE,
                )
            )
            return True
        except Exception:
            logger.exception("failed to send approval checklist", extra={"job_id": job.id})
            return False

    async def _send_approval_poll(self, job: Job) -> bool:
        if self._approval_polls is None:
            return False
        try:
            sent = await self._bot.send_poll(
                chat_id=self._owner_chat_id,
                question=f"How should I handle job {job.id}?",
                options=list(APPROVAL_POLL_OPTIONS),
                is_anonymous=False,
                allows_multiple_answers=False,
            )
            if sent.poll is None:
                raise RuntimeError("send_poll response has no poll payload")
            self._approval_polls.register(
                ApprovalPoll(
                    poll_id=sent.poll.id,
                    job_id=job.id,
                    chat_id=self._owner_chat_id,
                    message_id=sent.message_id,
                )
            )
            return True
        except Exception:
            logger.exception("failed to send approval poll", extra={"job_id": job.id})
            return False

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
            await self._send_assistant_poll_if_needed(job, natural)
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

    async def _send_assistant_poll_if_needed(self, job: Job, summary_text: str) -> None:
        if self._assistant_polls is None or not summary_text:
            return
        candidate = _extract_poll_candidate(summary_text)
        if candidate is None:
            return
        try:
            sent = await self._bot.send_poll(
                chat_id=self._owner_chat_id,
                question=candidate.question,
                options=list(candidate.options),
                is_anonymous=False,
                allows_multiple_answers=candidate.allows_multiple_answers,
            )
            if sent.poll is None:
                raise RuntimeError("send_poll response has no poll payload")
            self._assistant_polls.register(
                AssistantPoll(
                    poll_id=sent.poll.id,
                    source_job_id=job.id,
                    chat_id=self._owner_chat_id,
                    message_id=sent.message_id,
                    question=candidate.question,
                    options=candidate.options,
                    allows_multiple_answers=candidate.allows_multiple_answers,
                )
            )
        except Exception:
            logger.exception("failed to send assistant poll", extra={"job_id": job.id})

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
