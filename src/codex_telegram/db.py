from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
  telegram_user_id INTEGER PRIMARY KEY,
  is_owner INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL,
  mode TEXT NOT NULL,
  session_name TEXT,
  prompt TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  needs_approval INTEGER NOT NULL DEFAULT 0,
  approved_by INTEGER,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  summary_text TEXT,
  error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT,
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id_ts ON job_events(job_id, timestamp);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artifacts_job_id ON artifacts(job_id);

CREATE TABLE IF NOT EXISTS sessions (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  pid INTEGER,
  started_at TEXT,
  last_seen_at TEXT,
  metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS chat_state (
  chat_id INTEGER PRIMARY KEY,
  active_session_name TEXT,
  updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(query, params)
            self._conn.commit()
            return cur

    def query_one(self, query: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(query, params)
            return cur.fetchone()

    def query_all(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(query, params)
            return cur.fetchall()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
