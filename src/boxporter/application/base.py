"""Command base types shared by the storage facade and the command layer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from boxporter.core.errors import BoxPorterError

if TYPE_CHECKING:
    from boxporter.storage.store import Store


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str
    data: dict[str, object] = field(default_factory=dict)
    replayed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "message": self.message, "data": self.data}


class Command(ABC):
    """An idempotent write command executed inside one transaction.

    Subclasses declare a ``command`` verb, ``aggregate_type``, the set of
    ``allowed_actors`` and implement ``execute``. Data mutations must go
    through repositories / the event store on the transaction connection.
    """

    command: ClassVar[str]
    aggregate_type: ClassVar[str]
    allowed_actors: ClassVar[frozenset[str]] = frozenset(
        {"system", "user", "daemon"}
    )

    @property
    @abstractmethod
    def actor_type(self) -> str: ...

    @property
    @abstractmethod
    def aggregate_id(self) -> str: ...

    @abstractmethod
    def execute(self, store: Store) -> CommandResult:
        raise NotImplementedError


class CommandFailed(BoxPorterError):
    """Semantic command failure (state machine violation, validation, ...)."""
