from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import Database
from .models import Artifact, Job, JobMode, JobStatus, RiskLevel, SessionRecord, SessionStatus, parse_timestamp, serialize_payload


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _to_job(row: Any) -> Job:
    return Job(
        id=int(row["id"]),
        status=JobStatus(row["status"]),
        mode=JobMode(row["mode"]),
        prompt=row["prompt"],
        created_at=parse_timestamp(row["created_at"]) or datetime.now(UTC),
        updated_at=parse_timestamp(row["updated_at"]) or datetime.now(UTC),
        session_name=row["session_name"],
        risk_level=RiskLevel(row["risk_level"]),
        needs_approval=bool(row["needs_approval"]),
        approved_by=row["approved_by"],
        started_at=parse_timestamp(row["started_at"]),
        finished_at=parse_timestamp(row["finished_at"]),
        exit_code=row["exit_code"],
        summary_text=row["summary_text"],
        error_text=row["error_text"],
    )


def _to_artifact(row: Any) -> Artifact:
    return Artifact(
        id=int(row["id"]),
        job_id=int(row["job_id"]),
        kind=row["kind"],
        path=Path(row["path"]),
        size_bytes=int(row["size_bytes"]),
        sha256=row["sha256"],
    )


def _to_session(row: Any) -> SessionRecord:
    return SessionRecord(
        name=row["name"],
        status=SessionStatus(row["status"]),
        pid=row["pid"],
        started_at=parse_timestamp(row["started_at"]),
        last_seen_at=parse_timestamp(row["last_seen_at"]),
        metadata_json=row["metadata_json"],
    )


class Repository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def ensure_owner(self, owner_user_id: int) -> None:
        created_at = _now_iso()
        self._db.execute(
            """
            INSERT INTO users(telegram_user_id, is_owner, created_at)
            VALUES(?, 1, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET is_owner=1
            """,
            (owner_user_id, created_at),
        )

    def create_job(
        self,
        prompt: str,
        mode: JobMode,
        session_name: str | None,
        risk_level: RiskLevel,
        needs_approval: bool,
        status: JobStatus = JobStatus.QUEUED,
    ) -> Job:
        now = _now_iso()
        cur = self._db.execute(
            """
            INSERT INTO jobs(
              created_at, updated_at, status, mode, session_name, prompt,
              risk_level, needs_approval
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                status.value,
                mode.value,
                session_name,
                prompt,
                risk_level.value,
                int(needs_approval),
            ),
        )
        return self.get_job(int(cur.lastrowid))

    def get_job(self, job_id: int) -> Job:
        row = self._db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise KeyError(f"Job not found: {job_id}")
        return _to_job(row)

    def list_jobs(self, limit: int = 20) -> list[Job]:
        rows = self._db.query_all(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [_to_job(r) for r in rows]

    def count_jobs_by_status(self) -> dict[str, int]:
        rows = self._db.query_all(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status",
        )
        return {str(r["status"]): int(r["count"]) for r in rows}

    def reserve_next_runnable_job(self) -> Job | None:
        with self._db.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status=? AND (needs_approval=0 OR approved_by IS NOT NULL)
                ORDER BY id ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            now = _now_iso()
            updated = conn.execute(
                """
                UPDATE jobs
                SET status=?, updated_at=?, started_at=COALESCE(started_at, ?)
                WHERE id=? AND status=?
                """,
                (
                    JobStatus.RUNNING.value,
                    now,
                    now,
                    int(row["id"]),
                    JobStatus.QUEUED.value,
                ),
            )
            if updated.rowcount != 1:
                return None
            new_row = conn.execute("SELECT * FROM jobs WHERE id=?", (int(row["id"]),)).fetchone()
            if new_row is None:
                return None
            return _to_job(new_row)

    def set_job_status(
        self,
        job_id: int,
        status: JobStatus,
        *,
        summary_text: str | None = None,
        error_text: str | None = None,
        exit_code: int | None = None,
        approved_by: int | None = None,
        finished: bool = False,
    ) -> Job:
        now = _now_iso()
        finished_at = now if finished else None
        self._db.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?, summary_text=COALESCE(?, summary_text),
                error_text=COALESCE(?, error_text), exit_code=COALESCE(?, exit_code),
                approved_by=COALESCE(?, approved_by),
                finished_at=COALESCE(?, finished_at)
            WHERE id=?
            """,
            (
                status.value,
                now,
                summary_text,
                error_text,
                exit_code,
                approved_by,
                finished_at,
                job_id,
            ),
        )
        return self.get_job(job_id)

    def cancel_job(self, job_id: int) -> Job:
        now = _now_iso()
        self._db.execute(
            """
            UPDATE jobs
            SET status=?, updated_at=?, finished_at=?
            WHERE id=? AND status IN (?, ?, ?)
            """,
            (
                JobStatus.CANCELED.value,
                now,
                now,
                job_id,
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
                JobStatus.AWAITING_APPROVAL.value,
            ),
        )
        return self.get_job(job_id)

    def approve_job(self, job_id: int, user_id: int) -> Job:
        now = _now_iso()
        self._db.execute(
            """
            UPDATE jobs
            SET status=?, approved_by=?, updated_at=?
            WHERE id=? AND status=?
            """,
            (
                JobStatus.QUEUED.value,
                user_id,
                now,
                job_id,
                JobStatus.AWAITING_APPROVAL.value,
            ),
        )
        return self.get_job(job_id)

    def reject_job(self, job_id: int, user_id: int) -> Job:
        now = _now_iso()
        self._db.execute(
            """
            UPDATE jobs
            SET status=?, approved_by=?, updated_at=?, finished_at=?
            WHERE id=? AND status=?
            """,
            (
                JobStatus.REJECTED.value,
                user_id,
                now,
                now,
                job_id,
                JobStatus.AWAITING_APPROVAL.value,
            ),
        )
        return self.get_job(job_id)

    def append_event(self, job_id: int, event_type: str, payload: dict[str, Any] | None = None) -> None:
        now = _now_iso()
        self._db.execute(
            """
            INSERT INTO job_events(job_id, timestamp, event_type, payload_json)
            VALUES(?, ?, ?, ?)
            """,
            (
                job_id,
                now,
                event_type,
                serialize_payload(payload),
            ),
        )

    def list_events(self, job_id: int, limit: int = 100) -> list[tuple[str, str, str | None]]:
        rows = self._db.query_all(
            """
            SELECT timestamp, event_type, payload_json
            FROM job_events
            WHERE job_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (job_id, limit),
        )
        return [(r["timestamp"], r["event_type"], r["payload_json"]) for r in rows]

    def add_artifact(self, job_id: int, kind: str, path: Path, size_bytes: int, sha256: str) -> Artifact:
        cur = self._db.execute(
            """
            INSERT INTO artifacts(job_id, kind, path, size_bytes, sha256)
            VALUES(?, ?, ?, ?, ?)
            """,
            (job_id, kind, str(path), size_bytes, sha256),
        )
        row = self._db.query_one("SELECT * FROM artifacts WHERE id=?", (int(cur.lastrowid),))
        if row is None:
            raise RuntimeError("Artifact insert failed")
        return _to_artifact(row)

    def list_artifacts(self, job_id: int) -> list[Artifact]:
        rows = self._db.query_all("SELECT * FROM artifacts WHERE job_id=? ORDER BY id ASC", (job_id,))
        return [_to_artifact(r) for r in rows]

    def get_artifact(self, artifact_id: int) -> Artifact:
        row = self._db.query_one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if row is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return _to_artifact(row)

    def upsert_session(
        self,
        name: str,
        status: SessionStatus,
        pid: int | None,
        metadata_json: str | None,
    ) -> SessionRecord:
        now = _now_iso()
        self._db.execute(
            """
            INSERT INTO sessions(name, status, pid, started_at, last_seen_at, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                status=excluded.status,
                pid=excluded.pid,
                last_seen_at=excluded.last_seen_at,
                metadata_json=excluded.metadata_json,
                started_at=COALESCE(sessions.started_at, excluded.started_at)
            """,
            (name, status.value, pid, now, now, metadata_json),
        )
        return self.get_session(name)

    def get_session(self, name: str) -> SessionRecord:
        row = self._db.query_one("SELECT * FROM sessions WHERE name=?", (name,))
        if row is None:
            raise KeyError(f"Session not found: {name}")
        return _to_session(row)

    def list_sessions(self) -> list[SessionRecord]:
        rows = self._db.query_all("SELECT * FROM sessions ORDER BY name ASC")
        return [_to_session(r) for r in rows]

    def touch_session(self, name: str) -> None:
        now = _now_iso()
        self._db.execute(
            "UPDATE sessions SET last_seen_at=? WHERE name=?",
            (now, name),
        )
