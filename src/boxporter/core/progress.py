"""Deterministic progress scoring from machine signals (plan §8.4).

Pure function over observed signal tuples; never calls a model. A low
score is a *diagnosis trigger*, not a kill switch.
"""

from __future__ import annotations

from collections.abc import Iterable


def progress_score(signals: Iterable[str], window_seconds: float) -> float:
    """Score a window of machine signals.

    Signals are short tokens: ``checkpoint``, ``test_improved``,
    ``git_change``, ``root_cause``, ``tool_result``, ``repeated_error``,
    ``repeated_tool``, ``oscillating_diff``, ``no_signal``.
    """
    weights = {
        "checkpoint": 3.0,
        "test_improved": 3.0,
        "git_change": 2.0,
        "root_cause": 2.0,
        "tool_result": 1.0,
        "repeated_error": -2.0,
        "repeated_tool": -2.0,
        "oscillating_diff": -3.0,
        "no_signal": -4.0,
    }
    score = 0.0
    seen = 0
    for signal in signals:
        weight = weights.get(signal)
        if weight is None:
            continue
        seen += 1
        score += weight
    if seen == 0 and window_seconds > 0:
        score += weights["no_signal"]
    return score


def is_negative_signal(signal: str) -> bool:
    return signal in {"repeated_error", "repeated_tool", "oscillating_diff", "no_signal"}


def dominant_negative_signals(signals: Iterable[str]) -> list[str]:
    negatives = [signal for signal in signals if is_negative_signal(signal)]
    return sorted(set(negatives))
