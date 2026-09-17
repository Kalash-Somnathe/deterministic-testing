"""The operations a simulated process can yield.

A process in `deterministic_testing` is an ordinary Python generator function. It cannot block,
sleep, or do I/O directly. Instead it *yields a description of what it wants*, and
the scheduler decides when -- and in what order relative to every other process --
that want is satisfied.

That inversion is the whole trick. Because the process hands control back at every
interesting point, the scheduler owns every interleaving decision, and every one of
those decisions is drawn from the seed. Compare with real threads, where the
decision belongs to the OS and is unrecoverable.

The honest cost, stated up front: interleaving is only explored **at yield points**.
Two processes mutating a shared list between yields will never be interleaved by
this scheduler, so a true data race on shared memory is invisible to it. This tool
is built for message-passing, timeouts and retry logic; for memory races you want a
thread sanitiser, not this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generator, Optional

__all__ = [
    "Op",
    "Send",
    "Recv",
    "Sleep",
    "Now",
    "Random",
    "Yield",
    "Log",
    "Spawn",
    "Message",
    "ProcessGen",
]


@dataclass(frozen=True, slots=True)
class Message:
    """An envelope moving through the simulated network.

    ``seq`` is a monotonic integer assigned at send time. It is the stable
    identity used by the fault plan during shrinking, which is why it is assigned
    by a counter and never by ``id()``, ``uuid4()`` or ``hash()`` -- all three of
    which vary between processes and would make a recorded fault plan
    unreplayable.
    """

    seq: int
    sender: str
    recipient: str
    payload: dict[str, Any]
    sent_at: int
    """Virtual time (microseconds) the message was handed to the network."""
    deliver_at: int
    """Virtual time the network intends to deliver it."""
    is_duplicate: bool = False
    """True for the second and later copies produced by a DUPLICATE fault."""

    def summary(self) -> dict[str, Any]:
        """Compact form used in trace events. Keep this small: it is hashed."""
        detail: dict[str, Any] = {
            "seq": self.seq,
            "to": self.recipient,
            "payload": self.payload,
        }
        if self.is_duplicate:
            detail["dup"] = True
        return detail


class Op:
    """Marker base class for everything a process may yield."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Send(Op):
    """Hand a message to the network. Does not block.

    The process stays runnable. Delivery time, and whether the message is dropped,
    delayed or duplicated, is entirely the network's decision -- which is to say,
    the seed's decision.
    """

    recipient: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Recv(Op):
    """Wait for a message. Yields the :class:`Message`, or ``None`` on timeout.

    ``timeout`` is in seconds of *virtual* time, so a 30-second timeout costs
    nothing to test. ``None`` means wait forever, which is how you get an
    honest-to-goodness deadlock report rather than a hung test run.

    ``match`` gives Erlang-style **selective receive**: only messages satisfying
    the predicate are considered, and everything else stays in the mailbox in
    order. Without it, a process awaiting a database reply would happily consume
    the next job that arrived from the queue -- a modelling artefact that has
    nothing to do with the bug under investigation. The predicate must be pure;
    it is called during scheduling, so a side effect in there would be a source
    of non-determinism the guard cannot see.
    """

    timeout: Optional[float] = None
    match: Optional[Callable[["Message"], bool]] = None


@dataclass(frozen=True, slots=True)
class Sleep(Op):
    """Suspend for ``duration`` seconds of virtual time."""

    duration: float


@dataclass(frozen=True, slots=True)
class Now(Op):
    """Read the virtual clock. Yields an integer number of microseconds.

    Processes must use this rather than ``time.time()``. The runtime guard makes
    that not merely a convention but an enforced rule.
    """


@dataclass(frozen=True, slots=True)
class Random(Op):
    """Draw a uniform float in [0, 1) from the simulation's application stream.

    Systems under test often need randomness of their own (jittered backoff, load
    balancing). Routing it through the scheduler keeps it inside the seed, and
    keeps it in a *separate* sub-stream so that application draws do not shift
    scheduling decisions.
    """


@dataclass(frozen=True, slots=True)
class Yield(Op):
    """Voluntarily offer the scheduler a chance to run someone else.

    This is how you tell the framework "a real system could be preempted here".
    Placing these honestly is the main modelling skill required to use the tool,
    and the main thing that limits what it can find.
    """


@dataclass(frozen=True, slots=True)
class Log(Op):
    """Append an application-level event to the trace. Does not yield control."""

    label: str
    detail: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Spawn(Op):
    """Start a new process mid-run."""

    name: str
    generator: Generator[Op, Any, None]


ProcessGen = Generator[Op, Any, None]
"""A simulated process: a generator that yields Ops and is sent results back."""
