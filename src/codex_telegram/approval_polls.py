from __future__ import annotations

from dataclasses import dataclass

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


class ApprovalPollStore:
    def __init__(self) -> None:
        self._by_poll_id: dict[str, ApprovalPoll] = {}
        self._poll_id_by_job: dict[int, str] = {}

    def register(self, poll: ApprovalPoll) -> None:
        old_poll_id = self._poll_id_by_job.get(poll.job_id)
        if old_poll_id:
            self._by_poll_id.pop(old_poll_id, None)
        self._by_poll_id[poll.poll_id] = poll
        self._poll_id_by_job[poll.job_id] = poll.poll_id

    def get(self, poll_id: str) -> ApprovalPoll | None:
        return self._by_poll_id.get(poll_id)

    def pop(self, poll_id: str) -> ApprovalPoll | None:
        poll = self._by_poll_id.pop(poll_id, None)
        if poll is None:
            return None
        current = self._poll_id_by_job.get(poll.job_id)
        if current == poll_id:
            self._poll_id_by_job.pop(poll.job_id, None)
        return poll

    def pop_for_job(self, job_id: int) -> ApprovalPoll | None:
        poll_id = self._poll_id_by_job.pop(job_id, None)
        if poll_id is None:
            return None
        return self._by_poll_id.pop(poll_id, None)
