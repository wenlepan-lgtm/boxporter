"""Secret scanning for evidence packaging (ADR-008, basic edition).

Deterministic pattern checks over text/artifacts. Findings block sealing
and emit SECURITY_FINDING events; the full redaction pipeline (Context
Pack scanning, Secret Reference resolution) lands with Phase 5/6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ),
    (
        "aws-access-key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "openai-key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|token|access[_-]?key)\b"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{16,}"
        ),
    ),
    (
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
)


@dataclass(frozen=True)
class SecretFinding:
    file: str
    pattern: str
    line: int | None = None
    snippet: str | None = None


def scan_text(text: str, *, label: str = "<text>") -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for name, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start = match.start()
            line = text.count("\n", 0, start) + 1
            snippet = text[max(0, start - 20) : start + 20].replace("\n", "\\n")
            findings.append(
                SecretFinding(file=label, pattern=name, line=line, snippet=snippet)
            )
    return findings


def scan_file(path: Path) -> list[SecretFinding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return scan_text(text, label=str(path))


def scan_files(paths: list[Path]) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for path in paths:
        findings.extend(scan_file(path))
    return findings
