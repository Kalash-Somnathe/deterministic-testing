"""The virtual clock.

Time in a simulation is not measured, it is *decided*. The scheduler moves the
clock forward only when no process can make progress; then it jumps straight to
the next instant at which something is scheduled to happen. Nothing ever sleeps.

Two consequences worth stating plainly, because this is the easiest part of the
project to explain and the most immediately useful:

* A 30-second retry timeout costs zero wall-clock time to test. Ten thousand
  interleavings of an hour-long timeout scenario run in a few seconds.
* Timing bugs stop being flaky. "The ack arrived one microsecond after the
  timeout fired" becomes a scheduling decision derived from the seed, not a race
  against the operating system.

Representation: virtual time is an **integer count of microseconds**, not a float
number of seconds. Floats accumulate representation error under repeated addition
(0.1 + 0.2 != 0.3); that error is itself deterministic, but it makes traces ugly
and equality comparisons fragile. Integers make the trace exact and comparisons
trivial. The public API accepts seconds and converts once, at the boundary.
"""

from __future__ import annotations

__all__ = ["MICROS_PER_SECOND", "VirtualClock", "to_micros", "to_seconds"]

MICROS_PER_SECOND = 1_000_000


def to_micros(seconds: float | int) -> int:
    """Convert a user-facing duration in seconds to internal microseconds.

    Rounding happens exactly once, here at the boundary. Every arithmetic
    operation after this point is integer arithmetic, and therefore exact.
    """
    if seconds < 0:
        raise ValueError(f"durations must be non-negative, got {seconds}")
    return int(round(float(seconds) * MICROS_PER_SECOND))


def to_seconds(micros: int) -> float:
    """Convert internal microseconds back to seconds. For display only."""
    return micros / MICROS_PER_SECOND


class VirtualClock:
    """A monotonically non-decreasing integer clock under explicit control.

    There is deliberately no ``tick()`` and no ``sleep()``. The only way time
    moves is :meth:`advance_to`, called by the scheduler once it has established
    that no process is runnable. That restriction is what turns time from an
    input into a derived quantity.
    """

    __slots__ = ("_now",)

    def __init__(self, start_micros: int = 0) -> None:
        self._now = int(start_micros)

    @property
    def now(self) -> int:
        """Current virtual time, in microseconds since simulation start."""
        return self._now

    @property
    def now_seconds(self) -> float:
        """Current virtual time in seconds. Display only, never compare on this."""
        return to_seconds(self._now)

    def advance_to(self, micros: int) -> int:
        """Jump the clock forward to ``micros``. Returns the elapsed amount.

        Refuses to move backwards. A backwards jump means the scheduler chose to
        run an event that was already in the past, which is always a scheduler
        bug rather than something to paper over, so it raises loudly instead of
        clamping.
        """
        if micros < self._now:
            raise ValueError(
                f"virtual clock cannot move backwards: now={self._now}, requested={micros}"
            )
        elapsed = micros - self._now
        self._now = micros
        return elapsed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"VirtualClock(now={self._now}us / {self.now_seconds:.6f}s)"
