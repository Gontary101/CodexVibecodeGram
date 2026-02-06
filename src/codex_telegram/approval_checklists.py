from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

APPROVAL_TASK_APPROVE = 1
APPROVAL_TASK_REJECT = 2
APPROVAL_TASK_REVISE = 3
APPROVAL_CHECKLIST_TASKS: tuple[tuple[int, str], ...] = (
    (APPROVAL_TASK_APPROVE, "Approve and run"),
    (APPROVAL_TASK_REJECT, "Reject"),
    (APPROVAL_TASK_REVISE, "Suggest changes"),
)


@dataclass(slots=True)
class ApprovalChecklist:
    job_id: int
    chat_id: int
    message_id: int
    approve_task_id: int = APPROVAL_TASK_APPROVE
    reject_task_id: int = APPROVAL_TASK_REJECT
    revise_task_id: int = APPROVAL_TASK_REVISE


class ApprovalChecklistPersistence(Protocol):
    def list_approval_checklists(self) -> list[ApprovalChecklist]: ...

    def save_approval_checklist(self, checklist: ApprovalChecklist) -> None: ...

    def delete_approval_checklist(self, chat_id: int, message_id: int) -> None: ...


class ApprovalChecklistStore:
    def __init__(self, persistence: ApprovalChecklistPersistence | None = None) -> None:
        self._by_key: dict[tuple[int, int], ApprovalChecklist] = {}
        self._key_by_job: dict[int, tuple[int, int]] = {}
        self._persistence = persistence
        if persistence is not None:
            for checklist in persistence.list_approval_checklists():
                self._register_local(checklist)

    def _register_local(self, checklist: ApprovalChecklist) -> None:
        key = (checklist.chat_id, checklist.message_id)
        old_key = self._key_by_job.get(checklist.job_id)
        if old_key and old_key != key:
            self._by_key.pop(old_key, None)
        self._by_key[key] = checklist
        self._key_by_job[checklist.job_id] = key

    def register(self, checklist: ApprovalChecklist) -> None:
        key = (checklist.chat_id, checklist.message_id)
        old_key = self._key_by_job.get(checklist.job_id)
        if old_key and old_key != key:
            self._by_key.pop(old_key, None)
            if self._persistence is not None:
                self._persistence.delete_approval_checklist(old_key[0], old_key[1])
        self._by_key[key] = checklist
        self._key_by_job[checklist.job_id] = key
        if self._persistence is not None:
            self._persistence.save_approval_checklist(checklist)

    def get(self, chat_id: int, message_id: int) -> ApprovalChecklist | None:
        return self._by_key.get((chat_id, message_id))

    def pop(self, chat_id: int, message_id: int) -> ApprovalChecklist | None:
        key = (chat_id, message_id)
        checklist = self._by_key.pop(key, None)
        if checklist is None:
            return None
        current = self._key_by_job.get(checklist.job_id)
        if current == key:
            self._key_by_job.pop(checklist.job_id, None)
        if self._persistence is not None:
            self._persistence.delete_approval_checklist(chat_id, message_id)
        return checklist

    def pop_for_job(self, job_id: int) -> ApprovalChecklist | None:
        key = self._key_by_job.get(job_id)
        if key is None:
            return None
        return self.pop(key[0], key[1])
