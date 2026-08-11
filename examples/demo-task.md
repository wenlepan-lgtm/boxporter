# Objective

Add a `/healthz` endpoint and a deterministic regression test.

## Acceptance criteria

1. The endpoint returns HTTP 200 and a versioned JSON body.
2. The existing test suite remains green.
3. `result.md` lists changed files and `verify.md` contains the real test command.
4. An independent reviewer verifies the exact submitted evidence hash.
