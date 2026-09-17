"""The simulated network, and the faults it injects.

Real networks drop, delay, reorder and duplicate. Real systems are supposed to
cope. This module makes those events happen on demand, from the seed, so that
"cope" becomes a testable claim rather than a hope.

Two modes, and the distinction is what makes shrinking possible:

**Sample mode** -- fault decisions are drawn from the fault RNG sub-stream, and
every decision that deviated from the default is *recorded* into a
:class:`FaultPlan`.

**Plan mode** -- the network consults a supplied :class:`FaultPlan` and touches no
RNG at all. Faults are keyed by the message's monotonic sequence number.

Given a failing seed, we record its plan, then re-run subsets of that plan looking
for the smallest subset that still fails. Because plan mode is RNG-free on the
network side, "the same subset" always means the same faults on the same messages.

The honest caveat, which belongs here rather than buried in a README: removing a
fault changes how many messages exist, so sequence numbers after the removal point
no longer refer to the same messages. Shrinking is therefore a *search over
configurations*, each candidate re-run and re-checked, not a surgical edit of a
fixed trace. It reliably produces a smaller failing scenario; it does not prove
minimality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .clock import to_micros
from .ops import Message
from .rng import DeterministicRandom

__all__ = [
    "FaultKind",
    "Fault",
    "FaultPlan",
    "FaultConfig",
    "Network",
]


class FaultKind:
    """Fault categories, as plain strings so plans serialise to JSON cleanly."""

    DROP = "DROP"
    DUPLICATE = "DUPLICATE"
    DELAY = "DELAY"


@dataclass(frozen=True, slots=True, order=True)
class Fault:
    """One recorded fault decision, keyed by message sequence number.

    ``value`` carries the parameter where a fault has one (extra delay in
    microseconds for DELAY, copy count for DUPLICATE) and is 0 otherwise.
    """

    seq: int
    kind: str
    value: int = 0

    def describe(self) -> str:
        if self.kind == FaultKind.DELAY:
            return f"DELAY(msg={self.seq}, +{self.value}us)"
        if self.kind == FaultKind.DUPLICATE:
            return f"DUPLICATE(msg={self.seq}, copies={self.value})"
        return f"DROP(msg={self.seq})"


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """An ordered, immutable set of fault decisions.

    Stored as a tuple rather than a set: iteration order of a ``set`` is not
    guaranteed stable across processes under hash randomisation, and a plan whose
    order wobbles is a plan that does not replay.
    """

    faults: tuple[Fault, ...] = ()

    @classmethod
    def of(cls, faults: Iterable[Fault]) -> "FaultPlan":
        return cls(tuple(sorted(faults, key=lambda f: (f.seq, f.kind, f.value))))

    def __len__(self) -> int:
        return len(self.faults)

    def __iter__(self):
        return iter(self.faults)

    def for_seq(self, seq: int) -> tuple[Fault, ...]:
        return tuple(f for f in self.faults if f.seq == seq)

    def subset(self, keep: Iterable[Fault]) -> "FaultPlan":
        return FaultPlan.of(keep)

    def describe(self) -> str:
        if not self.faults:
            return "(no faults)"
        return "\n".join(f"  - {f.describe()}" for f in self.faults)

    def to_json(self) -> list[dict[str, int | str]]:
        return [{"seq": f.seq, "kind": f.kind, "value": f.value} for f in self.faults]

    @classmethod
    def from_json(cls, data: Iterable[dict]) -> "FaultPlan":
        return cls.of(Fault(int(d["seq"]), str(d["kind"]), int(d.get("value", 0))) for d in data)


@dataclass(slots=True)
class FaultConfig:
    """Probabilities and ranges for sample mode.

    ``base_latency`` is not a fault: it is the ordinary cost of the wire, applied
    to every message deterministically. Keeping it separate from ``DELAY`` faults
    means a shrunk plan describes only the *abnormal* behaviour, which is what a
    reader of the bug report cares about.
    """

    base_latency: float = 0.001
    """Seconds. Applied to every message, no randomness."""

    drop_probability: float = 0.0
    duplicate_probability: float = 0.0
    delay_probability: float = 0.0

    delay_min: float = 0.0
    """Seconds of *extra* latency when a DELAY fault fires."""
    delay_max: float = 0.0

    max_duplicates: int = 2
    """Total copies delivered when a DUPLICATE fault fires (including the original)."""

    partition_probability: float = 0.0
    """Chance per message of starting a partition on its (sender -> recipient) link."""
    partition_min: float = 0.0
    partition_max: float = 0.0
    """Seconds. While a link is partitioned, every message on it is DROPped."""

    @classmethod
    def none(cls) -> "FaultConfig":
        """A perfect network. Used by the 'conventional test suite' baseline."""
        return cls(base_latency=0.0)

    @classmethod
    def realistic(cls) -> "FaultConfig":
        """The profile used by the worked example.

        Numbers chosen to be unremarkable: a few percent loss, occasional
        duplicates, and delays on the order of a retry timeout. Nothing here is
        tuned to trigger the planted bug -- the point is that ordinary network
        weather finds it.
        """
        return cls(
            base_latency=0.001,
            drop_probability=0.04,
            duplicate_probability=0.03,
            delay_probability=0.15,
            delay_min=0.05,
            delay_max=1.20,
            max_duplicates=2,
            partition_probability=0.01,
            partition_min=0.20,
            partition_max=0.80,
        )


@dataclass(slots=True)
class _Partition:
    link: tuple[str, str]
    until: int


class Network:
    """Applies latency and faults to messages between simulated processes.

    The network does not deliver anything itself. It computes *when* each copy of
    a message should become deliverable and hands it back; the scheduler decides
    when to actually deliver it relative to everything else that is enabled. That
    split is what produces reordering without a dedicated "reorder" fault: two
    messages both eligible for delivery are two enabled transitions, and the
    scheduler picks between them from the seed.
    """

    __slots__ = ("_rng", "_config", "_plan", "_recorded", "_partitions", "_next_seq")

    def __init__(
        self,
        rng: DeterministicRandom,
        config: FaultConfig,
        plan: Optional[FaultPlan] = None,
    ) -> None:
        self._rng = rng
        self._config = config
        self._plan = plan
        self._recorded: list[Fault] = []
        self._partitions: list[_Partition] = []
        self._next_seq = 0

    @property
    def in_plan_mode(self) -> bool:
        return self._plan is not None

    def next_seq(self) -> int:
        self._next_seq += 1
        return self._next_seq

    def recorded_plan(self) -> FaultPlan:
        """Every fault that actually fired during this run."""
        return FaultPlan.of(self._recorded)

    # -- the one interesting method -------------------------------------

    def dispatch(
        self,
        *,
        seq: int,
        sender: str,
        recipient: str,
        payload: dict,
        now: int,
    ) -> tuple[list[Message], list[Fault]]:
        """Decide the fate of one send.

        Returns ``(copies_to_deliver, faults_applied)``. An empty copies list means
        the message was dropped. Faults are returned rather than logged internally
        so that the caller owns all trace writing, keeping event ordering in one
        place.
        """
        base = to_micros(self._config.base_latency)
        applied: list[Fault] = []

        if self._plan is not None:
            planned = self._plan.for_seq(seq)
            if any(f.kind == FaultKind.DROP for f in planned):
                drop = next(f for f in planned if f.kind == FaultKind.DROP)
                return [], [drop]
            extra = sum(f.value for f in planned if f.kind == FaultKind.DELAY)
            applied.extend(f for f in planned if f.kind == FaultKind.DELAY)
            copies = 1
            for fault in planned:
                if fault.kind == FaultKind.DUPLICATE:
                    copies = max(copies, fault.value)
                    applied.append(fault)
            return (
                self._build_copies(seq, sender, recipient, payload, now, base + extra, copies),
                applied,
            )

        # -- sample mode -------------------------------------------------
        # Order of RNG consumption below is part of the seed contract. Changing
        # it invalidates every previously recorded seed, so it is deliberately
        # fixed: partition expiry, partition start, drop, delay, duplicate.
        self._expire_partitions(now)
        link = (sender, recipient)

        if self._is_partitioned(link):
            fault = Fault(seq, FaultKind.DROP)
            self._recorded.append(fault)
            return [], [fault]

        if self._config.partition_probability > 0.0 and self._rng.chance(
            self._config.partition_probability
        ):
            duration = self._rng.integer(
                to_micros(self._config.partition_min), to_micros(self._config.partition_max)
            )
            self._partitions.append(_Partition(link, now + duration))
            fault = Fault(seq, FaultKind.DROP)
            self._recorded.append(fault)
            return [], [fault]

        if self._rng.chance(self._config.drop_probability):
            fault = Fault(seq, FaultKind.DROP)
            self._recorded.append(fault)
            return [], [fault]

        extra = 0
        if self._rng.chance(self._config.delay_probability):
            extra = self._rng.integer(
                to_micros(self._config.delay_min), to_micros(self._config.delay_max)
            )
            fault = Fault(seq, FaultKind.DELAY, extra)
            self._recorded.append(fault)
            applied.append(fault)

        copies = 1
        if self._rng.chance(self._config.duplicate_probability):
            copies = self._config.max_duplicates
            fault = Fault(seq, FaultKind.DUPLICATE, copies)
            self._recorded.append(fault)
            applied.append(fault)

        return (
            self._build_copies(seq, sender, recipient, payload, now, base + extra, copies),
            applied,
        )

    # -- helpers ---------------------------------------------------------

    def _build_copies(
        self,
        seq: int,
        sender: str,
        recipient: str,
        payload: dict,
        now: int,
        latency: int,
        copies: int,
    ) -> list[Message]:
        """Build the message copies. Duplicates share the sequence number.

        Sharing ``seq`` is intentional: a duplicate is the *same* message arriving
        twice, and the fault plan needs to name it once. The ``is_duplicate`` flag
        distinguishes copies in the trace.
        """
        out = [
            Message(
                seq=seq,
                sender=sender,
                recipient=recipient,
                payload=payload,
                sent_at=now,
                deliver_at=now + latency,
                is_duplicate=False,
            )
        ]
        for index in range(1, copies):
            # Later copies arrive slightly later, so that the two copies are not
            # simultaneous. One microsecond per copy is enough to make the trace
            # readable without changing any timeout outcome.
            out.append(
                Message(
                    seq=seq,
                    sender=sender,
                    recipient=recipient,
                    payload=payload,
                    sent_at=now,
                    deliver_at=now + latency + index,
                    is_duplicate=True,
                )
            )
        return out

    def _expire_partitions(self, now: int) -> None:
        self._partitions = [p for p in self._partitions if p.until > now]

    def _is_partitioned(self, link: tuple[str, str]) -> bool:
        return any(p.link == link for p in self._partitions)
