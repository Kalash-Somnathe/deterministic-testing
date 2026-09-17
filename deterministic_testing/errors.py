"""Exception types.

Each of these represents a distinct *finding*, not merely a crash. The search
driver treats them differently: an invariant violation is the thing we are
hunting, a deadlock is a genuine bug in the system under test, a step-limit
overrun usually means livelock, and a determinism violation means the framework
itself is broken and every result is suspect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .trace import Trace

__all__ = [
    "DeterministicTestingError",
    "InvariantViolation",
    "Deadlock",
    "StepLimitExceeded",
    "DeterminismViolation",
    "NonDeterminismLeak",
]


class DeterministicTestingError(Exception):
    """Base for everything this framework raises."""


class SimulationFailure(DeterministicTestingError):
    """A failure discovered *inside* a simulation run, carrying its evidence."""

    def __init__(self, message: str, *, seed: int, step: int, trace: "Optional[Trace]" = None) -> None:
        super().__init__(message)
        self.message = message
        self.seed = seed
        self.step = step
        self.trace = trace

    def __str__(self) -> str:
        return f"{self.message} (seed={self.seed}, step={self.step})"


class InvariantViolation(SimulationFailure):
    """A user-declared invariant returned False after a scheduler step.

    This is the framework's whole purpose: the seed on this exception is a
    permanent, portable reproduction of a concurrency bug.
    """

    def __init__(
        self,
        name: str,
        *,
        seed: int,
        step: int,
        detail: str = "",
        trace: "Optional[Trace]" = None,
    ) -> None:
        message = f"invariant {name!r} violated" + (f": {detail}" if detail else "")
        super().__init__(message, seed=seed, step=step, trace=trace)
        self.name = name
        self.detail = detail


class Deadlock(SimulationFailure):
    """No process can run, no message is in flight, no timer will fire.

    Distinct from normal completion: at least one process is still alive and
    waiting for something that will never arrive. In a real system this is the
    "everything is hung and nothing is on fire" outage.
    """


class StepLimitExceeded(SimulationFailure):
    """The run exceeded its step budget.

    Usually livelock (processes making moves but no progress) or a workload that
    is simply too large. Deliberately a hard error rather than a silent
    truncation: a truncated run could hide a violation that was two steps away
    and report a clean seed, which is the one outcome worse than a false alarm.
    """


class DeterminismViolation(DeterministicTestingError):
    """The same seed produced two different traces. The framework is broken."""


class NonDeterminismLeak(DeterministicTestingError):
    """The system under test reached for a real clock or unseeded randomness.

    Raised by the runtime guard. This is the single most important error message
    in the project, because a leak here silently destroys replayability: the seed
    in a bug report would no longer be a complete description of the run.
    """
