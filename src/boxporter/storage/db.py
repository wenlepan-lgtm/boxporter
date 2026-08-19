"""SQLite connection management and forward-only migrations.

Migrations are embedded SQL scripts applied in order, tracked by
``PRAGMA user_version`` (ADR-002). Never edit an applied migration;
changes require a new migration.

Durability settings: WAL, foreign keys on, synchronous=FULL (the V1
reliability target says committed transactions must never be lost).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 0001: core protocol kernel V2 (projects, goals, tasks, attempts, runs,
    # events, operations).
    (
        """
        CREATE TABLE projects (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          workspace_root TEXT NOT NULL,
          status TEXT NOT NULL,
          config_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE goals (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id),
          title TEXT NOT NULL,
          outcome TEXT NOT NULL,
          success_criteria_json TEXT NOT NULL,
          progress REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id),
          goal_id TEXT REFERENCES goals(id),
          title TEXT NOT NULL,
          objective TEXT NOT NULL,
          state TEXT NOT NULL,
          priority INTEGER NOT NULL,
          risk_level TEXT NOT NULL,
          current_attempt INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL,
          timeout_seconds INTEGER NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          task_spec_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_tasks_project_state ON tasks(project_id, state)",
        "CREATE INDEX idx_tasks_goal ON tasks(goal_id)",
        """
        CREATE TABLE attempts (
          id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES tasks(id),
          number INTEGER NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(task_id, number)
        )
        """,
        """
        CREATE TABLE runs (
          id TEXT PRIMARY KEY,
          attempt_id TEXT NOT NULL REFERENCES attempts(id),
          role TEXT NOT NULL,
          runner TEXT NOT NULL,
          provider TEXT,
          model TEXT,
          identity TEXT NOT NULL,
          session_id TEXT NOT NULL,
          state TEXT NOT NULL,
          checkpoint_ref TEXT,
          started_at TEXT,
          ended_at TEXT,
          stop_reason TEXT
        )
        """,
        "CREATE INDEX idx_runs_attempt ON runs(attempt_id)",
        "CREATE INDEX idx_runs_state ON runs(state)",
        """
        CREATE TABLE events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          aggregate_type TEXT NOT NULL,
          aggregate_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          actor_type TEXT NOT NULL,
          actor_id TEXT,
          payload_json TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          causation_id TEXT,
          correlation_id TEXT
        )
        """,
        """
        CREATE INDEX idx_events_aggregate
          ON events(aggregate_type, aggregate_id, seq)
        """,
        """
        CREATE TABLE operations (
          operation_id TEXT PRIMARY KEY,
          command TEXT NOT NULL,
          aggregate_type TEXT NOT NULL,
          aggregate_id TEXT NOT NULL,
          result_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """,
    ),
    # 0002: explicit leases with fencing tokens + attempt recovery counters
    # (ADR-004).
    (
        """
        CREATE TABLE leases (
          run_id TEXT PRIMARY KEY REFERENCES runs(id),
          task_id TEXT NOT NULL REFERENCES tasks(id),
          role TEXT NOT NULL,
          owner_instance TEXT NOT NULL,
          fencing_token INTEGER NOT NULL,
          pid INTEGER,
          heartbeat_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX idx_leases_task_role
          ON leases(task_id, role)
        """,
        "CREATE INDEX idx_leases_expires ON leases(expires_at)",
        """
        ALTER TABLE attempts ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0
        """,
    ),
    # 0003: frozen submissions, independent reviews, artifacts (ADR-005,
    # ADR-006) + worktree recording on runs.
    (
        "ALTER TABLE runs ADD COLUMN worktree TEXT",
        """
        CREATE TABLE submissions (
          id TEXT PRIMARY KEY,
          attempt_id TEXT NOT NULL REFERENCES attempts(id),
          submission_sha256 TEXT NOT NULL UNIQUE,
          head_commit TEXT NOT NULL,
          git_tree_sha TEXT NOT NULL,
          manifest_json TEXT NOT NULL,
          frozen_at TEXT NOT NULL,
          invalidated_at TEXT
        )
        """,
        "CREATE INDEX idx_submissions_attempt ON submissions(attempt_id)",
        """
        CREATE TABLE reviews (
          id TEXT PRIMARY KEY,
          submission_id TEXT NOT NULL REFERENCES submissions(id),
          run_id TEXT NOT NULL REFERENCES runs(id),
          result TEXT NOT NULL,
          report_ref TEXT NOT NULL,
          evidence_sha256 TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_reviews_submission ON reviews(submission_id)",
        """
        CREATE TABLE artifacts (
          id TEXT PRIMARY KEY,
          run_id TEXT REFERENCES runs(id),
          submission_id TEXT REFERENCES submissions(id),
          kind TEXT NOT NULL,
          uri TEXT NOT NULL,
          sha256 TEXT NOT NULL,
          size_bytes INTEGER,
          redaction_status TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_artifacts_submission ON artifacts(submission_id)",
        "CREATE INDEX idx_artifacts_run ON artifacts(run_id)",
    ),
    # 0004: control-plane settings and device sessions for the Web console
    # (ADR-012).
    (
        """
        CREATE TABLE settings (
          key TEXT PRIMARY KEY,
          value_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE web_sessions (
          id TEXT PRIMARY KEY,
          device_label TEXT NOT NULL,
          created_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          reauth_until TEXT,
          revoked INTEGER NOT NULL DEFAULT 0
        )
        """,
    ),
    # 0005: usage metering, external blockers with condition probes, and
    # deduplicated notifications (plan §11.4, §9.1, §10.7).
    (
        """
        CREATE TABLE usage (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES runs(id),
          tokens_in INTEGER NOT NULL DEFAULT 0,
          tokens_out INTEGER NOT NULL DEFAULT 0,
          cost REAL NOT NULL DEFAULT 0,
          tool_calls INTEGER NOT NULL DEFAULT 0,
          recorded_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_usage_run ON usage(run_id)",
        "CREATE INDEX idx_usage_recorded ON usage(recorded_at)",
        """
        CREATE TABLE blockers (
          id TEXT PRIMARY KEY,
          task_id TEXT NOT NULL REFERENCES tasks(id),
          reason TEXT NOT NULL,
          probe_command_json TEXT NOT NULL DEFAULT '[]',
          probe_interval_seconds INTEGER NOT NULL DEFAULT 900,
          next_probe_at TEXT,
          created_at TEXT NOT NULL,
          resolved_at TEXT
        )
        """,
        "CREATE INDEX idx_blockers_task ON blockers(task_id)",
        """
        CREATE TABLE notifications (
          id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          dedup_key TEXT NOT NULL UNIQUE,
          payload_json TEXT NOT NULL,
          channel TEXT NOT NULL DEFAULT 'log',
          created_at TEXT NOT NULL
        )
        """,
    ),
    # 0006: retry backoff + error fingerprints, prompt versioning on runs,
    # approvals and gated project memory (plan §9.3, §19.3, §12.3).
    (
        "ALTER TABLE attempts ADD COLUMN next_retry_at TEXT",
        "ALTER TABLE attempts ADD COLUMN error_fingerprint TEXT",
        "ALTER TABLE runs ADD COLUMN prompt_sha TEXT",
        """
        CREATE TABLE approvals (
          id TEXT PRIMARY KEY,
          task_id TEXT REFERENCES tasks(id),
          run_id TEXT REFERENCES runs(id),
          action TEXT NOT NULL,
          target TEXT NOT NULL,
          risk_level TEXT NOT NULL,
          max_uses INTEGER NOT NULL DEFAULT 1,
          used_count INTEGER NOT NULL DEFAULT 0,
          expires_at TEXT NOT NULL,
          status TEXT NOT NULL,
          requested_by TEXT,
          decided_by TEXT,
          decided_at TEXT,
          created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_approvals_task ON approvals(task_id)",
        """
        CREATE TABLE memory_items (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id),
          kind TEXT NOT NULL,
          content TEXT NOT NULL,
          source TEXT NOT NULL,
          source_ref TEXT,
          expires_at TEXT,
          created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_memory_project ON memory_items(project_id)",
    ),
)


class Database:
    """Thread-local connections: each thread gets its own connection to the
    same SQLite file (WAL + busy_timeout arbitrate). All threads in one
    process share the same migration state."""

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        connection = getattr(self._local, "conn", None)
        if connection is None:
            connection = self._open_connection()
            self._local.conn = connection
        return connection

    def _open_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        self._migrate(conn)
        return conn

    def open(self) -> None:
        self.conn  # noqa: B018 - open the thread-local connection eagerly

    def migrate(self) -> None:
        self._migrate(self.conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(current, len(MIGRATIONS)):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in MIGRATIONS[index]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {index + 1}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        connection = getattr(self._local, "conn", None)
        if connection is not None:
            connection.close()
            self._local.conn = None

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Explicit transaction scope on the current thread's connection."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def __enter__(self) -> Database:  # noqa: PYI034 - Python 3.10 has no typing.Self
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
