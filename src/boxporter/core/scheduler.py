"""Deterministic scheduler: the zero-token tick (plan §11.1, §11.5).

``Scheduler.tick`` never calls a model by itself. It:
1. applies watchdog findings through the recovery engine;
2. retries failed tasks within recovery budgets (deterministic);
3. starts runs via the runner registry when capacity allows;
4. otherwise returns ``idle`` with ``model_call=False``.

A run start counts as a model call only when the runner's capabilities
declare ``requires_model``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from boxporter.application.base import CommandFailed
from boxporter.application.commands import (
    BeginNextAttempt,
    BlockTask,
    FailRun,
    MarkRunRunning,
    StartExecutorRun,
    StartReviewerRun,
)
from boxporter.core.budget import BudgetService
from boxporter.core.clock import now_iso
from boxporter.core.errors import BoxPorterError, NotFoundError
from boxporter.core.gitworktree import WorktreeManager
from boxporter.core.guard import HIGH_RISK_ACTIONS, RunnerExecutionGuard
from boxporter.core.lease import LeaseConflict, LeaseManager
from boxporter.core.notify import Notifier
from boxporter.core.policy import PolicySnapshot
from boxporter.core.probe import ProbeRunner
from boxporter.core.recovery import RecoveryAction, RecoveryEngine
from boxporter.core.schemas import Run, Task
from boxporter.core.state import RUN_ACTIVE_STATES, RunState
from boxporter.core.watchdog import WatchDog, WatchFinding
from boxporter.runners.base import (
    RunHandle,
    RunnerAdapter,
    RunnerRegistry,
    RunnerUnsupported,
    RunSpec,
)
from boxporter.storage.events import ActorType, EventType
from boxporter.storage.store import Store

EXECUTOR_ROLE = "executor"
REVIEWER_ROLE = "reviewer"

RISK_PENALTY = {"low": 0, "medium": 5, "high": 15}


@dataclass(frozen=True)
class SchedulingPolicy:
    mode: str = "SUPERVISED"  # SUPERVISED / AWAY / PAUSED
    max_concurrent: int = 1
    allowed_risk_levels: frozenset[str] = frozenset({"low", "medium"})
    auto_review: bool = True
    runner: str | None = None  # None -> first healthy registered runner
    max_recoveries_per_attempt: int = 2
    daily_token_budget: int = 2000000

    def validate(self) -> None:
        if self.mode not in {"SUPERVISED", "AWAY", "PAUSED"}:
            raise BoxPorterError(f"unknown scheduling mode: {self.mode}")
        if self.max_concurrent < 1:
            raise BoxPorterError("max_concurrent must be >= 1")


@dataclass(frozen=True)
class TickResult:
    action: str
    model_call: bool
    detail: dict[str, object] = field(default_factory=dict)


class Scheduler:
    def __init__(
        self,
        store: Store,
        runners: RunnerRegistry,
        lease_manager: LeaseManager,
        watchdog: WatchDog,
        recovery: RecoveryEngine,
        policy: SchedulingPolicy | None = None,
        worktrees_root: Path | None = None,
        budget_service: BudgetService | None = None,
        reports_dir_name: str = "reports",
    ):
        self.store = store
        self.runners = runners
        self.leases = lease_manager
        self.watchdog = watchdog
        self.recovery = recovery
        self.policy = policy or SchedulingPolicy()
        self.policy.validate()
        self.worktrees_root = worktrees_root
        self.worktrees = (
            WorktreeManager(worktrees_root) if worktrees_root is not None else None
        )
        self.notifier = Notifier(store)
        self.guard = RunnerExecutionGuard(store)
        self.reports_dir_name = reports_dir_name
        self.handles: dict[str, RunHandle] = {}
        self._session_ids: dict[str, str] = {}
        self._seen_events: dict[str, object] = {}
        self.budget = budget_service or BudgetService(store)

    def apply_policy(self, snapshot: PolicySnapshot) -> None:
        """Re-read operating policy from settings (mode, risk, budgets)."""
        self.policy = SchedulingPolicy(
            mode=snapshot.mode,
            max_concurrent=snapshot.max_concurrent,
            allowed_risk_levels=snapshot.allowed_risk_levels,
            auto_review=snapshot.auto_review,
            max_recoveries_per_attempt=snapshot.max_recoveries_per_attempt,
            daily_token_budget=snapshot.daily_token_budget,
        )

    # -- main entry --------------------------------------------------------

    def tick(self, now: object | None = None) -> TickResult:
        from datetime import datetime, timezone

        current: datetime = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        model_call = False

        self._run_probes(current)
        self._check_budgets()

        for finding in self.watchdog.check():
            decision = self.recovery.decide(finding)
            if decision.action == RecoveryAction.FAIL_RUN:
                self._apply_failure(finding.run_id, decision.reason)
            else:
                self._record_finding(finding)

        for task_id in self._failed_tasks():
            decision = self.recovery.after_failure(
                task_id,
                max_recoveries=self.policy.max_recoveries_per_attempt,
                now=current,
            )
            if decision.action == RecoveryAction.BEGIN_NEXT_ATTEMPT:
                self._execute_quietly(
                    BeginNextAttempt(task_id=task_id, actor_type=ActorType.DAEMON)
                )
            elif decision.action == RecoveryAction.STOP_AND_NOTIFY:
                self._execute_quietly(
                    BlockTask(
                        task_id=task_id,
                        reason=f"recovery budget exhausted: {decision.reason}",
                        actor_type=ActorType.DAEMON,
                    )
                )
                self.notifier.recovery_stop(task_id, decision.reason)

        if self.policy.mode == "PAUSED":
            return TickResult(action="paused", model_call=False)

        started_executor = self._schedule_executors()
        started_reviewer = self._schedule_reviewers()
        self._observe_active_runs_and_advance()
        if started_executor or started_reviewer:
            model_call = any(
                self._runner_requires_model(run_id)
                for run_id in started_executor + started_reviewer
            )
            return TickResult(
                action="started_runs",
                model_call=model_call,
                detail={
                    "executor_runs": started_executor,
                    "reviewer_runs": started_reviewer,
                },
            )
        return TickResult(action="idle", model_call=False)

    # -- observation-driven lifecycle closure (fix-guide P0-B) -------------

    def _observe_active_runs_and_advance(self) -> None:
        """Close the observation loop: terminal adapter states advance the
        task state machine inside the tick (SUCCEEDED executor -> freeze
        submission; SUCCEEDED reviewer -> record verdict; CRASHED/TIMED_OUT
        -> FailRun). Handles for terminal runs are dropped."""
        for run_id in list(self.handles):
            runner = self._runner_for(run_id)
            if runner is None:
                continue
            handle = self.handles[run_id]
            observation = runner.inspect(handle)
            if observation.state in RUN_ACTIVE_STATES:
                continue
            try:
                run = self.store.runs.get(self.store.db.conn, run_id)
            except NotFoundError:
                self.handles.pop(run_id, None)
                continue
            if observation.state == RunState.SUCCEEDED:
                if run.role == EXECUTOR_ROLE:
                    self._auto_submit_executor(run)
                elif run.role == REVIEWER_ROLE:
                    self._auto_record_review(run)
            elif observation.state in {RunState.CRASHED, RunState.TIMED_OUT}:
                reason = str(observation.detail.get("reason") or "observed-terminal")
                if run.role == EXECUTOR_ROLE:
                    self._apply_failure(run_id, reason)
                else:
                    self._fail_reviewer_run(run_id, reason)
            self._record_terminal_event(run_id, observation.state)
            self.handles.pop(run_id, None)
            self._seen_events.pop(run_id, None)

    def _auto_submit_executor(self, run: object) -> None:
        from pathlib import Path

        from boxporter.application.commands import SubmitExecutorRun

        assert isinstance(run, Run)
        if run.worktree is None:
            self._apply_failure(run.id, "no-worktree-for-submit")
            return
        report_dir = Path(run.worktree) / self.reports_dir_name
        if not all(
            (report_dir / name).is_file() for name in ("result.md", "verify.md", "executor.md")
        ):
            self._apply_failure(run.id, "reports-missing-in-worktree")
            return
        try:
            self.store.execute(
                SubmitExecutorRun(
                    run_id=run.id,
                    report_dir=str(report_dir),
                    worktree=str(run.worktree),
                    actor_type=ActorType.DAEMON,
                ),
                operation_id=f"tick-submit-{run.id}",
            )
        except CommandFailed:
            pass  # already terminal (e.g. manual submit); nothing to do

    def _auto_record_review(self, run: object) -> None:
        import json
        from pathlib import Path

        from boxporter.application.commands import ReviewTask
        from boxporter.core.state import RunState as RS

        assert isinstance(run, Run)
        if run.worktree is None:
            self._fail_reviewer_run(run.id, "no-worktree-for-review")
            return
        review_dir = Path(run.worktree) / self.reports_dir_name
        review_md = review_dir / "review.md"
        evidence_path = review_dir / "review_evidence.json"
        if not review_md.is_file():
            self._fail_reviewer_run(run.id, "review-artifacts-missing")
            return
        verdict = self._parse_review_verdict(review_md.read_text(encoding="utf-8"))
        if verdict is None:
            self._fail_reviewer_run(run.id, "review-verdict-unparseable")
            return
        required_changes: tuple[str, ...] = ()
        if evidence_path.is_file():
            try:
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                evidence = None
            if isinstance(evidence, dict):
                raw_changes = evidence.get("required_changes")
                if isinstance(raw_changes, list):
                    required_changes = tuple(str(item) for item in raw_changes)
        attempt = self.store.attempts.get(self.store.db.conn, run.attempt_id)
        task = self.store.tasks.get(self.store.db.conn, attempt.task_id)
        self.store.runs.update_state(self.store.db.conn, run.id, RS.SUCCEEDED)
        try:
            self.store.execute(
                ReviewTask(
                    task_id=task.id,
                    reviewer_run_id=run.id,
                    result=verdict,
                    required_changes=required_changes,
                    review_dir=str(review_dir),
                    actor_type=ActorType.DAEMON,
                ),
                operation_id=f"tick-review-{run.id}",
            )
        except CommandFailed as exc:
            self.notifier.recovery_stop(task.id, f"review-rejected: {exc}")

    @staticmethod
    def _parse_review_verdict(text: str) -> str | None:
        import re

        match = re.search(
            r"^\s*##?\s*结论\s*[:：]?\s*(PASS|REVISE|BLOCKED)\s*$",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        if match is not None:
            return match.group(1).upper()
        for verdict in ("PASS", "REVISE", "BLOCKED"):
            if re.search(rf"^\s*{verdict}\s*$", text, flags=re.MULTILINE):
                return verdict
        return None

    def _fail_reviewer_run(self, run_id: str, reason: str) -> None:
        conn = self.store.db.conn
        run = self.store.runs.get(conn, run_id)
        self.store.runs.update_state(conn, run_id, RunState.CRASHED, stop_reason=reason)
        attempt = self.store.attempts.get(conn, run.attempt_id)
        task = self.store.tasks.get(conn, attempt.task_id)
        self.notifier.recovery_stop(task.id, f"reviewer-failed: {reason}")

    def _record_terminal_event(self, run_id: str, state: object) -> None:
        from boxporter.core.state import RunState as RS

        assert isinstance(state, RS)
        conn = self.store.db.conn
        with self.store.db.transaction():
            self.store.events.append(
                conn,
                aggregate_type="run",
                aggregate_id=run_id,
                event_type=EventType.RUN_OBSERVED_TERMINAL,
                actor_type=ActorType.DAEMON,
                payload={"observed_state": state.value},
            )

    def send_message(self, run_id: str, message: str, action: str | None = None) -> bool:
        """Send a message into a run; high-risk actions consume an approval
        first (fix-guide P1-E). Returns False when rejected."""
        run = self.store.runs.get(self.store.db.conn, run_id)
        attempt = self.store.attempts.get(self.store.db.conn, run.attempt_id)
        if action is not None and action in HIGH_RISK_ACTIONS:
            result = self.guard.consume(task_id=attempt.task_id, action=action)
            if not result.allowed:
                return False
        handle = self.handles.get(run_id)
        runner = self._runner_for(run_id)
        if handle is None or runner is None:
            return False
        runner.send(handle, message)
        return True

    # -- probes and budgets ------------------------------------------------

    def _run_probes(self, now: datetime) -> None:
        ProbeRunner(self.store, now=now).run_due()

    def _check_budgets(self) -> None:
        """Fail runs whose task token budget is exhausted and notify once."""
        conn = self.store.db.conn
        rows = conn.execute("SELECT id FROM tasks WHERE state = 'WORKING'").fetchall()
        for row in rows:
            task = self.store.tasks.get(conn, str(row["id"]))
            check = self.budget.task_over_budget(task)
            if check.allowed:
                continue
            run_row = conn.execute(
                "SELECT r.id FROM runs r JOIN attempts a ON a.id = r.attempt_id"
                " WHERE a.task_id = ? AND r.state = 'RUNNING' ORDER BY r.rowid LIMIT 1",
                (task.id,),
            ).fetchone()
            if run_row is not None:
                self.store.execute(
                    FailRun(
                        run_id=str(run_row["id"]),
                        kind="budget",
                        actor_type=ActorType.DAEMON,
                    ),
                    operation_id=f"tick-budget-{task.id}",
                )
            self.notifier.budget(task.id, check.used, check.limit)

    def _schedule_executors(self) -> list[str]:
        started: list[str] = []
        for task in self._ranked_ready_tasks():
            if task.risk_level not in self.policy.allowed_risk_levels:
                continue
            if task.risk_level == "high":
                # High-risk execution requires a scoped, approved approval
                # even when the policy admits the risk level (fix-guide P1-E).
                granted = self.guard.consume(
                    task_id=task.id, action="execute-high-risk-task"
                )
                if not granted.allowed:
                    continue
            if not self._has_capacity():
                break
            if self.store.runs.has_active_run(
                self.store.db.conn, task.id, EXECUTOR_ROLE
            ):
                continue
            budget_check = self.budget.can_start_run(
                task, self.policy.daily_token_budget
            )
            if not budget_check.allowed:
                continue
            runner = self._pick_runner()
            if runner is None:
                break
            session_id = self._next_session_id(task, EXECUTOR_ROLE)
            result = self.store.execute(
                StartExecutorRun(
                    task_id=task.id,
                    runner=runner.capabilities().name,
                    identity=f"executor:{runner.capabilities().name}",
                    session_id=session_id,
                    actor_type=ActorType.DAEMON,
                ),
                operation_id=f"tick-exec-{task.id}-a{task.current_attempt}",
            )
            if not result.ok:
                continue
            run_id = str(result.data["run_id"])
            self._session_ids[run_id] = session_id
            if self._launch(run_id, task, EXECUTOR_ROLE):
                started.append(run_id)
        return started

    def _schedule_reviewers(self) -> list[str]:
        if not self.policy.auto_review:
            return []
        started: list[str] = []
        conn = self.store.db.conn
        rows = conn.execute(
            "SELECT id FROM tasks WHERE state = 'REVIEW_PENDING'"
        ).fetchall()
        for row in rows:
            task = self.store.tasks.get(conn, str(row["id"]))
            if not self._has_capacity():
                break
            if self.store.runs.has_active_run(conn, task.id, REVIEWER_ROLE):
                continue
            runner = self._pick_runner()
            if runner is None:
                break
            session_id = self._next_session_id(task, REVIEWER_ROLE)
            result = self.store.execute(
                StartReviewerRun(
                    task_id=task.id,
                    runner=runner.capabilities().name,
                    identity=f"reviewer:{runner.capabilities().name}",
                    session_id=session_id,
                    actor_type=ActorType.DAEMON,
                ),
                operation_id=f"tick-rev-{task.id}-a{task.current_attempt}",
            )
            if not result.ok:
                continue
            run_id = str(result.data["run_id"])
            self._session_ids[run_id] = session_id
            if self._launch(run_id, task, REVIEWER_ROLE):
                started.append(run_id)
        return started

    def _launch(self, run_id: str, task: Task, role: str) -> bool:
        runner = self._runner_for(run_id)
        if runner is None:
            self._apply_failure(run_id, "no-runner")
            return False
        worktree_path = self._prepare_worktree(run_id, task, role)
        context_prompt = self._render_prompt(run_id, task, role)
        spec = RunSpec(
            run_id=run_id,
            task_id=task.id,
            attempt=task.current_attempt,
            role=role,
            workspace=(
                str(worktree_path) if worktree_path is not None else task.spec.workspace
            ),
            task=task.spec,
            session_id=self._session_ids[run_id],
            runner_profile=(
                task.spec.reviewer_profile
                if role == REVIEWER_ROLE
                else task.spec.executor_profile
            ),
            context_prompt=context_prompt,
        )
        try:
            handle = runner.start(spec)
        except (BoxPorterError, OSError) as exc:
            self._apply_failure(run_id, f"adapter-start-failed: {exc}")
            return False
        try:
            lease = self.leases.acquire(run_id, pid=handle.pid)
        except LeaseConflict as exc:
            self._apply_failure(run_id, f"lease-conflict: {exc}")
            return False
        self.handles[run_id] = handle
        try:
            result = self.store.execute(
                MarkRunRunning(run_id=run_id, actor_type=ActorType.DAEMON),
                operation_id=f"tick-running-{run_id}",
            )
        except CommandFailed:
            self.leases.release(run_id, lease.fencing_token)
            self._apply_failure(run_id, "run-could-not-start")
            return False
        return result.ok

    def _render_prompt(self, run_id: str, task: Task, role: str) -> str:
        """Render the versioned role prompt + Context Pack; pin its sha on
        the run so prompt changes never rewrite history (plan §7.3)."""
        import hashlib

        from boxporter.core.contextpack import build_context_pack
        from boxporter.core.prompts import PromptService

        prompt_service = PromptService(self.store)
        pack = build_context_pack(self.store, task, role)
        rendered = prompt_service.render(role, pack)
        self.store.runs.set_prompt_sha(
            self.store.db.conn,
            run_id,
            hashlib.sha256(rendered.encode()).hexdigest(),
        )
        return rendered

    def _prepare_worktree(self, run_id: str, task: Task, role: str) -> Path | None:
        """Create the run's isolated worktree (ADR-005, ADR-006).

        Executors get a fresh worktree; reviewers get a detached worktree at
        the frozen submission head commit (read-only review object)."""
        if self.worktrees is None:
            return None
        repo = Path(task.spec.workspace).resolve()
        if role == REVIEWER_ROLE:
            attempt = self.store.attempts.get_by_task_number(
                self.store.db.conn, task.id, task.current_attempt
            )
            submission = self.store.submissions.get_for_attempt(
                self.store.db.conn, attempt.id
            )
            base = submission.head_commit if submission is not None else None
        else:
            base = task.spec.base_commit
        name = f"{task.id}-a{task.current_attempt}-{role[0]}{run_id[-6:]}"
        worktree = self.worktrees.add_worktree(repo, name, base_commit=base)
        self.store.runs.set_worktree(self.store.db.conn, run_id, str(worktree))
        return worktree

    def _has_capacity(self) -> bool:
        row = self.store.db.conn.execute(
            "SELECT COUNT(*) AS count FROM runs WHERE state IN"
            " ('CREATED','STARTING','RUNNING','CHECKPOINTING','WAITING_APPROVAL','STALLED')"
        ).fetchone()
        return int(row["count"]) < self.policy.max_concurrent

    def _ranked_ready_tasks(self) -> list[Task]:
        conn = self.store.db.conn
        rows = conn.execute("SELECT id FROM tasks WHERE state = 'READY'").fetchall()
        tasks = [self.store.tasks.get(conn, str(row["id"])) for row in rows]
        tasks = [
            task
            for task in tasks
            if self.store.tasks.dependencies_satisfied(conn, task)
        ]
        return sorted(tasks, key=self._task_score, reverse=True)

    @staticmethod
    def _task_score(task: Task) -> float:
        return float(task.priority) - RISK_PENALTY.get(task.risk_level, 0)

    def _pick_runner(self) -> RunnerAdapter | None:
        name = self.policy.runner
        if name is not None:
            try:
                return self.runners.get(name)
            except RunnerUnsupported:
                return None
        names = self.runners.names()
        return self.runners.get(names[0]) if names else None

    # -- failure bookkeeping -----------------------------------------------

    def _apply_failure(self, run_id: str, reason: str) -> None:
        try:
            self.store.execute(
                FailRun(
                    run_id=run_id,
                    kind="crash",
                    stop_reason=reason,
                    actor_type=ActorType.DAEMON,
                ),
                operation_id=f"tick-fail-{run_id}-{reason}",
            )
        except CommandFailed:
            pass  # already terminal; nothing to do

    def _record_finding(self, finding: WatchFinding) -> None:
        self.watchdog.record_finding(finding)

    def _failed_tasks(self) -> list[str]:
        rows = self.store.db.conn.execute(
            "SELECT id FROM tasks WHERE state = 'FAILED'"
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def _execute_quietly(self, command: BeginNextAttempt | BlockTask) -> bool:
        try:
            result = self.store.execute(command)
        except CommandFailed:
            return False
        return result.ok

    # -- runner lookup and session ids -------------------------------------

    def _runner_for(self, run_id: str) -> RunnerAdapter | None:
        run = self.store.runs.get(self.store.db.conn, run_id)
        try:
            return self.runners.get(run.runner)
        except RunnerUnsupported:
            return None

    def _runner_requires_model(self, run_id: str) -> bool:
        runner = self._runner_for(run_id)
        return runner is not None and runner.capabilities().requires_model

    def _next_session_id(self, task: Task, role: str) -> str:
        conn = self.store.db.conn
        rows = conn.execute(
            "SELECT r.rowid FROM runs r JOIN attempts a ON a.id = r.attempt_id"
            " WHERE a.task_id = ? AND r.role = ?",
            (task.id, role),
        ).fetchall()
        return f"{role}_{task.id}_a{task.current_attempt}_r{len(rows) + 1}"

    # -- daemon loop helpers ------------------------------------------------

    def heartbeat_all(self) -> int:
        """Heartbeat leases whose adapter observations are alive, and record
        effective-progress signals when the runtime reports new activity."""
        count = 0
        for run_id in list(self.handles):
            handle = self.handles[run_id]
            lease = self.leases.get(run_id)
            runner = self._runner_for(run_id)
            if lease is None or runner is None:
                continue
            observation = runner.inspect(handle)
            if observation.state in RUN_ACTIVE_STATES:
                self.leases.heartbeat(run_id, lease.fencing_token)
                count += 1
            self._record_activity(run_id, observation)
        return count

    def _record_activity(self, run_id: str, observation: object) -> None:
        """Record an effective-progress signal when the runtime shows new
        activity (new events, token consumption or state-relevant changes)."""
        from boxporter.runners.base import RunObservation

        assert isinstance(observation, RunObservation)
        key: object
        if "events" in observation.detail:
            raw_events = observation.detail.get("events", 0)
            key = int(raw_events) if isinstance(raw_events, (int, str)) else 0
        else:
            key = (
                observation.state.value,
                observation.usage_tokens,
                observation.exit_code,
            )
        previous = self._seen_events.get(run_id)
        if previous is not None and previous != key:
            self.record_progress(run_id, "tool_result")
        self._seen_events[run_id] = key

    def record_progress(self, run_id: str, signal: str) -> None:
        conn = self.store.db.conn
        with self.store.db.transaction():
            self.store.events.append(
                conn,
                aggregate_type="run",
                aggregate_id=run_id,
                event_type=EventType.PROGRESS_SIGNAL,
                actor_type=ActorType.DAEMON,
                payload={"signal": signal, "at": now_iso()},
            )
