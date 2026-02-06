from pathlib import Path

from codex_telegram.db import Database
from codex_telegram.models import JobMode, JobStatus, RiskLevel
from codex_telegram.repository import Repository


def test_job_lifecycle_and_reserve(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.init_schema()
    repo = Repository(db)

    job = repo.create_job(
        prompt="echo hi",
        mode=JobMode.EPHEMERAL,
        session_name=None,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
    )

    reserved = repo.reserve_next_runnable_job()
    assert reserved is not None
    assert reserved.id == job.id
    assert reserved.status == JobStatus.RUNNING

    finished = repo.set_job_status(
        job.id,
        JobStatus.SUCCEEDED,
        summary_text="ok",
        exit_code=0,
        finished=True,
    )
    assert finished.status == JobStatus.SUCCEEDED
    assert finished.exit_code == 0


def test_approval_transitions(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.init_schema()
    repo = Repository(db)

    job = repo.create_job(
        prompt="sudo apt install htop",
        mode=JobMode.EPHEMERAL,
        session_name=None,
        risk_level=RiskLevel.MEDIUM,
        needs_approval=True,
    )

    awaiting = repo.set_job_status(job.id, JobStatus.AWAITING_APPROVAL)
    assert awaiting.status == JobStatus.AWAITING_APPROVAL

    approved = repo.approve_job(job.id, user_id=42)
    assert approved.status == JobStatus.QUEUED
    assert approved.approved_by == 42


def test_count_jobs_by_status(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.init_schema()
    repo = Repository(db)

    a = repo.create_job(
        prompt="echo 1",
        mode=JobMode.EPHEMERAL,
        session_name=None,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
    )
    b = repo.create_job(
        prompt="echo 2",
        mode=JobMode.EPHEMERAL,
        session_name=None,
        risk_level=RiskLevel.LOW,
        needs_approval=False,
    )

    repo.set_job_status(a.id, JobStatus.SUCCEEDED, finished=True, exit_code=0)
    repo.set_job_status(b.id, JobStatus.FAILED, finished=True, exit_code=1)

    counts = repo.count_jobs_by_status()

    assert counts["succeeded"] >= 1
    assert counts["failed"] >= 1


def test_chat_active_session_persistence(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.init_schema()
    repo = Repository(db)

    assert repo.get_active_session_for_chat(1001) is None

    repo.set_active_session_for_chat(1001, "session-a")
    assert repo.get_active_session_for_chat(1001) == "session-a"

    repo.set_active_session_for_chat(1001, "session-b")
    assert repo.get_active_session_for_chat(1001) == "session-b"

    repo.set_active_session_for_chat(1001, None)
    assert repo.get_active_session_for_chat(1001) is None
