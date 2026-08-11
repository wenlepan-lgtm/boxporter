#!/bin/sh
set -eu

ROOT="${1:-.boxporter-demo}"

boxporter --root "$ROOT" init
boxporter --root "$ROOT" add --id demo-healthz --title "Add health endpoint" --file examples/demo-task.md
boxporter --root "$ROOT" promote
boxporter --root "$ROOT" transition WORKING --handoff-to executor

printf '%s\n' '# Result' '' '- Added the endpoint and test.' >"$ROOT/reports/result.md"
printf '%s\n' '# Verification' '' '- `python -m unittest`: PASS' >"$ROOT/reports/verify.md"

boxporter --root "$ROOT" submit --author executor-demo
boxporter --root "$ROOT" review --result PASS --author reviewer-demo --content "Acceptance criteria verified."
boxporter --root "$ROOT" status
