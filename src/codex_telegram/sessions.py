from __future__ import annotations

import asyncio
import os
import shlex
import signal
from dataclasses import dataclass

from .config import Settings
from .models import SessionRecord, SessionStatus
from .repository import Repository


@dataclass(slots=True)
class SessionCreateResult:
    record: SessionRecord
    created: bool


class SessionManager:
    def __init__(self, repo: Repository, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def _build_boot_command(self, session_name: str) -> str:
        template = self._settings.codex_session_boot_cmd_template
        if not template:
            raise RuntimeError("CODEX_SESSION_BOOT_CMD_TEMPLATE is not configured")
        return template.format(
            session_name=session_name,
            session_name_quoted=shlex.quote(session_name),
        )

    async def create(self, session_name: str) -> SessionCreateResult:
        try:
            existing = self._repo.get_session(session_name)
            if existing.status == SessionStatus.ACTIVE:
                return SessionCreateResult(record=existing, created=False)
        except KeyError:
            existing = None

        pid: int | None = None
        metadata: str | None = None

        if self._settings.codex_session_boot_cmd_template:
            command = self._build_boot_command(session_name)
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            self._processes[session_name] = proc
            pid = proc.pid
            metadata = f"boot_command={command}"

        record = self._repo.upsert_session(
            name=session_name,
            status=SessionStatus.ACTIVE,
            pid=pid,
            metadata_json=metadata,
        )
        return SessionCreateResult(record=record, created=True)

    async def stop(self, session_name: str) -> SessionRecord:
        record = self._repo.get_session(session_name)

        proc = self._processes.pop(session_name, None)
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        elif record.pid:
            try:
                os.kill(record.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        return self._repo.upsert_session(
            name=session_name,
            status=SessionStatus.INACTIVE,
            pid=None,
            metadata_json=record.metadata_json,
        )

    def list_sessions(self) -> list[SessionRecord]:
        return self._repo.list_sessions()

    def is_session_active(self, session_name: str) -> bool:
        try:
            record = self._repo.get_session(session_name)
        except KeyError:
            return False
        return record.status == SessionStatus.ACTIVE
