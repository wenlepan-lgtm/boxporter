# Changelog

## 1.1.0 - 2026-08-19

- Add the V2 task, attempt, run, lease, event, and four-box lifecycle model.
- Add isolated executor/reviewer runs with worktree-bound submission manifests and acceptance gates.
- Add event-driven scheduling, retry backoff, token budgets, progress detection, circuit breaking,
  approvals, context packs, and crash recovery.
- Add command, DeepSeek Harness, and pinned OpenHands runner adapters.
- Add the FastAPI control plane, project-scoped Web console, run management, health components,
  blockers, and replayable SSE event cursors.
- Add SQLite migration, backup/restore, launchd, log rotation, and security operations guidance.
- Keep runtime credentials, SQLite databases, WAL/SHM files, logs, caches, and artifacts outside
  the Git repository.

## 0.2.0 - 2026-08-14

- Add dependency-aware task promotion with `depends_on`.
- Add bounded review convergence based on concrete required-change IDs.
- Pause non-converging review loops in `WAITING_USER` after two base rounds.
- Allow at most two progress-only extensions, with a hard cap of four reviews.
- Make revision history idempotent when the same submission is replayed after a crash.
- Preserve compatibility with roots initialized by BoxPorter 0.1.
- Publish current operational limitations in `KNOWN_ISSUES.md`.

## 0.1.0 - 2026-08-13

- Initial public release of the single-active-task box protocol.
- Content-addressed executor submissions and independent reviews.
- Atomic evidence archives, deduplicated polling, and vendor-neutral command hooks.
