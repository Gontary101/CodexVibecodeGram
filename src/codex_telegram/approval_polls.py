from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

APPROVAL_OPTION_APPROVE = 0
APPROVAL_OPTION_REJECT = 1
APPROVAL_OPTION_REVISE = 2
APPROVAL_POLL_OPTIONS: tuple[str, str, str] = (
    "Approve and run",
    "Reject",
    "Suggest changes",
)


@dataclass(slots=True)
class ApprovalPoll:
    poll_id: str
    job_id: int
    chat_id: int
    message_id: int


class ApprovalPollPersistence(Protocol):
    def list_approval_polls(self) -> list[ApprovalPoll]: ...

    def save_approval_poll(self, poll: ApprovalPoll) -> None: ...

    def delete_approval_poll(self, poll_id: str) -> None: ...


class ApprovalPollStore:
    def __init__(self, persistence: ApprovalPollPersistence | None = None) -> None:
        self._by_poll_id: dict[str, ApprovalPoll] = {}
        self._poll_id_by_job: dict[int, str] = {}
        self._persistence = persistence
        if persistence is not None:
            for poll in persistence.list_approval_polls():
                self._register_local(poll)

    def _register_local(self, poll: ApprovalPoll) -> None:
        old_poll_id = self._poll_id_by_job.get(poll.job_id)
        if old_poll_id and old_poll_id != poll.poll_id:
            self._by_poll_id.pop(old_poll_id, None)
        self._by_poll_id[poll.poll_id] = poll
        self._poll_id_by_job[poll.job_id] = poll.poll_id

    def register(self, poll: ApprovalPoll) -> None:
        old_poll_id = self._poll_id_by_job.get(poll.job_id)
        if old_poll_id and old_poll_id != poll.poll_id:
            self._by_poll_id.pop(old_poll_id, None)
            if self._persistence is not None:
                self._persistence.delete_approval_poll(old_poll_id)
        self._by_poll_id[poll.poll_id] = poll
        self._poll_id_by_job[poll.job_id] = poll.poll_id
        if self._persistence is not None:
            self._persistence.save_approval_poll(poll)

    def get(self, poll_id: str) -> ApprovalPoll | None:
        return self._by_poll_id.get(poll_id)

    def pop(self, poll_id: str) -> ApprovalPoll | None:
        poll = self._by_poll_id.pop(poll_id, None)
        if poll is None:
            return None
        current = self._poll_id_by_job.get(poll.job_id)
        if current == poll_id:
            self._poll_id_by_job.pop(poll.job_id, None)
        if self._persistence is not None:
            self._persistence.delete_approval_poll(poll_id)
        return poll

    def pop_for_job(self, job_id: int) -> ApprovalPoll | None:
        poll_id = self._poll_id_by_job.get(job_id)
        if poll_id is None:
            return None
        return self.pop(poll_id)
