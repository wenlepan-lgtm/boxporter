# Changelog

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
