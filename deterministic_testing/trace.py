"""The event trace: what actually happened, in order, canonically.

The trace is the framework's output. It is simultaneously the human-readable bug
report and the machine-checkable determinism proof, so its serialisation has to be
exact.

Hashing rule, stated once and enforced everywhere: **never use the builtin
hash()**. ``hash("abc")`` is salted per interpreter process by ``PYTHONHASHSEED``,
so a "trace hash" built on it would differ between two runs of the same seed on
the same machine. That is a false determinism failure that costs hours to
diagnose, and it is precisely the bug class this framework exists to catch --
shipping it in the foundations would be embarrassing. Everything here goes through
SHA-256 over UTF-8 bytes, which is a pure function of its input on every machine,
forever.

Payloads are serialised with ``json.dumps(..., sort_keys=True)`` for the same
family of reason: two dicts that compare equal must produce identical bytes
regardless of the order their keys happened to be inserted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from .clock import to_seconds

__all__ = ["Event", "Trace", "canonical_json"]


def canonical_json(value: Any) -> str:
    """Serialise a value to a stable string: order-independent and compact.

    ``sort_keys=True`` is the load-bearing argument. Python dicts preserve
    insertion order, so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` are equal
    but would serialise differently without it, producing two different trace
    digests for two runs that did exactly the same thing.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return repr(value)


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened, at one instant, attributed to one process.

    Frozen, because a trace that can be edited after the fact is not evidence.
    """

    step: int
    """Scheduler step number. Monotonic, starts at 1."""

    time_micros: int
    """Virtual time at which this happened."""

    kind: str
    """Short event category: SEND, DELIVER, RECV, DROP, DUP, SLEEP, SPAWN, ..."""

    process: str
    """Name of the process responsible."""

    detail: dict[str, Any] = field(default_factory=dict)
    """Structured payload. Always serialised with sorted keys."""

    def canonical(self) -> str:
        """The one true string form of this event: hash input and log line."""
        return (
            f"{self.step}|{self.time_micros}|{self.kind}|{self.process}|"
            f"{canonical_json(self.detail)}"
        )

    def pretty(self) -> str:
        """Human-readable single line, for bug reports."""
        detail = canonical_json(self.detail) if self.detail else ""
        return (
            f"[{self.step:>5}] t={to_seconds(self.time_micros):>10.6f}s "
            f"{self.kind:<9} {self.process:<14} {detail}"
        )


class Trace:
    """An append-only ordered log of :class:`Event` with a stable digest."""

    __slots__ = ("_events",)

    def __init__(self, events: Iterable[Event] | None = None) -> None:
        self._events: list[Event] = list(events) if events else []

    def record(self, event: Event) -> None:
        self._events.append(event)

    @property
    def events(self) -> list[Event]:
        return self._events

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __getitem__(self, index: int) -> Event:
        return self._events[index]

    def canonical_text(self) -> str:
        """The whole trace as one canonical string. The hash input."""
        return "\n".join(event.canonical() for event in self._events)

    def digest(self) -> str:
        """SHA-256 of the canonical text: the determinism fingerprint.

        Two runs of the same seed must produce byte-identical digests. That single
        assertion, repeated a hundred times, is the most important test in this
        repository. If it ever fails, every seed the tool has ever reported is
        void.
        """
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()

    def short_digest(self) -> str:
        return self.digest()[:16]

    def pretty(self, limit: int | None = None) -> str:
        events = self._events if limit is None else self._events[-limit:]
        return "\n".join(event.pretty() for event in events)

    def filtered(self, kinds: Iterable[str]) -> "Trace":
        wanted = tuple(kinds)
        return Trace(event for event in self._events if event.kind in wanted)

    def to_jsonl(self) -> str:
        """One JSON object per line. For saving a trace to disk as an artifact."""
        return "\n".join(
            json.dumps(
                {
                    "step": e.step,
                    "t_us": e.time_micros,
                    "kind": e.kind,
                    "process": e.process,
                    "detail": e.detail,
                },
                sort_keys=True,
                default=repr,
            )
            for e in self._events
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Trace({len(self._events)} events, digest={self.short_digest()})"
