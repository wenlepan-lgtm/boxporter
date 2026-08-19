"""BoxPorter error types shared across the control plane."""

from __future__ import annotations


class BoxPorterError(RuntimeError):
    """Base class for all BoxPorter errors."""


class IllegalTransitionError(BoxPorterError):
    """A state transition was attempted that is not in the legal transition table."""


class ConcurrencyError(BoxPorterError):
    """An optimistic-lock version check or unique-lease check failed."""


class ValidationError(BoxPorterError):
    """A task, project or spec failed schema validation."""


class NotFoundError(BoxPorterError):
    """A referenced aggregate does not exist."""


class IdempotencyConflict(BoxPorterError):
    """The same operation id was replayed with different parameters."""
