# Contributing

BoxPorter deliberately stays small. Before proposing a feature, check that it preserves:

1. human-readable task and report files;
2. deterministic, zero-token coordination when state is unchanged;
3. independent executor/reviewer roles;
4. crash-safe handoffs and content-addressed evidence;
5. no mandatory runtime dependency outside the Python standard library.

Run the local gate before opening a pull request:

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest -q
```

Bug reports should include the task state, the last relevant JSONL event, the command that
failed, and a minimal reproduction. Remove credentials and customer data before posting.
