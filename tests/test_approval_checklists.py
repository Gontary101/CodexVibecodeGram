from codex_telegram.approval_checklists import ApprovalChecklist, ApprovalChecklistStore


def test_approval_checklist_store_register_get_and_pop() -> None:
    store = ApprovalChecklistStore()
    checklist = ApprovalChecklist(job_id=9, chat_id=42, message_id=100)

    store.register(checklist)

    fetched = store.get(42, 100)
    assert fetched is not None
    assert fetched.job_id == 9
    assert store.pop_for_job(9) == checklist
    assert store.get(42, 100) is None
