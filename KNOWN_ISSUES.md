# Known issues and current limitations

These items were observed while using BoxPorter with Claude/GLM as executor and Codex as
independent reviewer. They are listed here rather than hidden behind a "fully autonomous"
claim.

## 1. Token telemetry is provider-specific

Claude Code and Codex TUI summaries may return an empty metrics object. BoxPorter can count
agent launches, but cannot yet report exact input, output, and cache tokens consistently.

## 2. Generic CLI adapters are still manual

The public package launches configured command arrays, but does not install or authenticate
Claude Code, Codex, GLM, or DeepSeek. Session resume, model aliases, effort levels, and
rate-limit recovery remain adapter-specific.

## 3. Review convergence is a heuristic

Version 0.2 uses concrete required-change counts instead of broad failed-gate counts. This
fixed a real false pause, but counting cannot prove semantic progress. The hard cap remains
four reviews, after which a human must decide whether to split, waive, or redesign the task.

## 4. Sleeping TUI processes may remain visible

Some interactive CLIs keep an idle parent process after a handoff finishes. BoxPorter state
is authoritative; process presence alone is not proof that tokens are still being consumed.

## 5. File-event schedulers can be noisy

macOS `launchd` WatchPaths can retrigger when the coordinator writes its own state files.
This normally costs zero model tokens because `tick` deduplicates the handoff, but it can
produce repetitive local logs. A native cross-platform event daemon is not included yet.

## 6. Evidence is not automatically redacted

Passed bundles are immutable and may contain customer names, internal addresses, commands,
or logs. Keep `.boxporter/` out of public repositories and run a secret/privacy scan before
sharing evidence.

## 7. Single-host trust model

BoxPorter assumes trusted local processes on one filesystem. It has no network API, tenant
isolation, distributed locking, or sandbox boundary. Use a workflow engine or message queue
for untrusted or multi-host execution.

## 8. Dependency cycles are not diagnosed automatically

Promotion is fail-closed: a task with unresolved dependencies stays pending. Version 0.2
does not distinguish a genuinely unfinished dependency from a missing task or dependency
cycle. `boxporter status` therefore cannot yet explain why a dependency graph is stuck.
