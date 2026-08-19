"""Store facade: database + event log + repositories + idempotent commands.

``Store.execute`` is the single write path for every command: it wraps the
command in a transaction, appends events inside that same transaction, and
records the operation id for idempotent replay (ADR-002).
"""

from __future__ import annotations

from boxporter.application.base import Command, CommandResult
from boxporter.core.errors import BoxPorterError
from boxporter.core.ids import new_id

from .db import Database
from .events import EventStore
from .metering import (
    ApprovalsRepo,
    BlockersRepo,
    MemoryRepo,
    NotificationsRepo,
    UsageRepo,
)
from .repositories import (
    ArtifactsRepo,
    AttemptsRepo,
    GoalsRepo,
    OperationsRepo,
    ProjectsRepo,
    ReviewsRepo,
    RunsRepo,
    SubmissionsRepo,
    TasksRepo,
)
from .web import SettingsRepo, WebSessionsRepo


class Store:
    def __init__(self, db: Database):
        self.db = db
        self.events = EventStore()
        self.projects = ProjectsRepo()
        self.goals = GoalsRepo()
        self.tasks = TasksRepo()
        self.attempts = AttemptsRepo()
        self.runs = RunsRepo()
        self.operations = OperationsRepo()
        self.submissions = SubmissionsRepo()
        self.reviews = ReviewsRepo()
        self.artifacts = ArtifactsRepo()
        self.settings = SettingsRepo()
        self.web_sessions = WebSessionsRepo()
        self.usage = UsageRepo()
        self.blockers = BlockersRepo()
        self.notifications = NotificationsRepo()
        self.approvals = ApprovalsRepo()
        self.memory = MemoryRepo()

    def execute(
        self, command: Command, operation_id: str | None = None
    ) -> CommandResult:
        op_id = operation_id or new_id("op")
        conn = self.db.conn
        with self.db.transaction():
            recorded = self.operations.get(conn, op_id)
            if recorded is not None:
                data = recorded.get("data")
                return CommandResult(
                    ok=bool(recorded.get("ok")),
                    message=str(recorded.get("message", "")),
                    data=dict(data) if isinstance(data, dict) else {},
                    replayed=True,
                )
            if command.actor_type not in command.allowed_actors:
                raise BoxPorterError(
                    f"actor {command.actor_type!r} not allowed to run {command.command}"
                )
            result = command.execute(self)
            self.operations.insert(
                conn,
                operation_id=op_id,
                command=command.command,
                aggregate_type=command.aggregate_type,
                aggregate_id=command.aggregate_id,
                result=result.to_dict(),
            )
            return result
