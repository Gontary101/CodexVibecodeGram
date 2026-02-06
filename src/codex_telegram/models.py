from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


class JobMode(StrEnum):
    EPHEMERAL = "ephemeral"
    SESSION = "session"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(slots=True)
class Job:
    id: int
    status: JobStatus
    mode: JobMode
    prompt: str
    created_at: datetime
    updated_at: datetime
    session_name: str | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    needs_approval: bool = False
    approved_by: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    summary_text: str | None = None
    error_text: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELED,
            JobStatus.REJECTED,
        }


@dataclass(slots=True)
class Artifact:
    id: int
    job_id: int
    kind: str
    path: Path
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class SessionRecord:
    name: str
    status: SessionStatus
    pid: int | None
    started_at: datetime | None
    last_seen_at: datetime | None
    metadata_json: str | None


@dataclass(slots=True)
class JobEvent:
    id: int
    job_id: int
    timestamp: datetime
    event_type: str
    payload_json: str | None


@dataclass(slots=True)
class ExecutionResult:
    exit_code: int
    stdout_path: Path
    stderr_path: Path
    summary: str
    error_text: str | None = None


@dataclass(slots=True)
class ExecutionContext:
    job: Job
    run_dir: Path
    approved: bool


@dataclass(slots=True)
class ExecutionPlan:
    command: str
    env_overrides: dict[str, str]


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def serialize_payload(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    import json

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
