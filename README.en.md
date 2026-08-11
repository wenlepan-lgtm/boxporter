# BoxPorter

> Move tasks, not entire conversations.

BoxPorter is a small, auditable coordination protocol for coding agents. Tasks are
ordinary Markdown documents moving through human-readable boxes. One agent implements
and verifies a change; another agent independently reviews the exact submitted evidence.

The coordinator itself is deterministic and costs no model tokens. It launches an agent
only for a new handoff or a bounded stale-task recovery event.

```text
pending/ -> active/current.md -> passed/
                    |
                    +----------> blocked/
```

## Highlights

- Human-readable Markdown tasks and reports
- One active task at a time
- Executor/reviewer separation
- Reviews bound to the SHA-256 of `result.md` and `verify.md`
- Atomic writes and atomic evidence-bundle archival
- Vendor-neutral command hooks
- Python standard library only

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

boxporter init
boxporter add --id demo --title "Fix a bug" --body "Fix the root cause and add a regression test."
boxporter promote
boxporter transition WORKING --handoff-to executor
```

Write the compact outcome to `.boxporter/reports/result.md` and real test evidence to
`.boxporter/reports/verify.md`, then submit:

```bash
boxporter submit --author executor
boxporter review --result PASS --author reviewer --content "All acceptance gates verified."
```

See the [Chinese README](README.md) and [protocol](docs/PROTOCOL.md) for the complete
workflow and automation configuration.

## License

MIT
