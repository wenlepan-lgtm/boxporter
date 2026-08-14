# BoxPorter task-box protocol

BoxPorter uses ordinary UTF-8 Markdown files with small, machine-readable front matter.
The filesystem is both the coordination substrate and the human audit surface.

## Boxes

- `boxes/pending/`: tasks waiting to start.
- `boxes/active/current.md`: the only task allowed to be in progress.
- `boxes/passed/`: immutable evidence bundles accepted by an independent reviewer.
- `boxes/blocked/`: tasks that require external authority or resources.

## State machine

```text
PENDING -> READY -> WORKING -> REVIEW_PENDING
                      ^              |
                      |              +-> REVISE
                      |              +-> INVALID
                      +------------------+

REVIEW_PENDING -> PASS -> passed/
WORKING -> BLOCKED -> blocked/
```

`PASS` requires a reviewer report tied to the exact SHA-256 identity of `result.md` and
`verify.md`. Changing either file after submission invalidates the review.

## Dependencies

Tasks may declare a comma-separated `depends_on` field. Promotion scans pending tasks in
stable order and selects the first task whose dependencies already exist in `passed/`.
An unresolved dependency never becomes active merely because it is the oldest task.

## Bounded review convergence

`REVISE` and `INVALID` reviews must provide concrete required-change IDs. The default
policy allows two review rounds. If the number of concrete remaining changes strictly
decreases, two additional rounds may be used. An unchanged/increased set, or a fourth
non-PASS review, moves the task to `WAITING_USER`. This prevents both premature stopping
and unbounded executor/reviewer loops.

## Atomicity

Documents are written to a temporary file in the destination directory, flushed with
`fsync`, and installed with `os.replace`. A passed task is first assembled as a staging
directory containing the task, result, verification, both agent reports, and a SHA-256
manifest; the complete directory is then atomically renamed into `passed/`. Readers
therefore see either no archive or one complete evidence bundle, never a partial handoff.

## Agent report contract

Executor and reviewer reports use `BOXPORTER_AGENT_REPORT_V1` and include:

- stable report ID and timestamp;
- author and role;
- task ID;
- result;
- submission SHA-256.
- JSON-encoded required-change IDs for non-PASS results.

The reviewer must not reuse the executor's conclusion. It independently checks the
acceptance criteria and evidence for the exact submission.

## Token behavior

`boxporter tick` is a deterministic local decision. With no state change it invokes no
model. A configured agent command is launched only for a new handoff or after a bounded
retry interval. A system scheduler can call `tick` every 20 minutes as a watchdog.
