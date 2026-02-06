from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AssistantPoll:
    poll_id: str
    source_job_id: int
    chat_id: int
    message_id: int
    question: str
    options: tuple[str, ...]
    allows_multiple_answers: bool = False


class AssistantPollStore:
    def __init__(self) -> None:
        self._by_poll_id: dict[str, AssistantPoll] = {}
        self._poll_id_by_job: dict[int, str] = {}

    def register(self, poll: AssistantPoll) -> None:
        old_poll_id = self._poll_id_by_job.get(poll.source_job_id)
        if old_poll_id:
            self._by_poll_id.pop(old_poll_id, None)
        self._by_poll_id[poll.poll_id] = poll
        self._poll_id_by_job[poll.source_job_id] = poll.poll_id

    def get(self, poll_id: str) -> AssistantPoll | None:
        return self._by_poll_id.get(poll_id)

    def pop(self, poll_id: str) -> AssistantPoll | None:
        poll = self._by_poll_id.pop(poll_id, None)
        if poll is None:
            return None
        current = self._poll_id_by_job.get(poll.source_job_id)
        if current == poll_id:
            self._poll_id_by_job.pop(poll.source_job_id, None)
        return poll

    def pop_for_job(self, source_job_id: int) -> AssistantPoll | None:
        poll_id = self._poll_id_by_job.pop(source_job_id, None)
        if poll_id is None:
            return None
        return self._by_poll_id.pop(poll_id, None)
