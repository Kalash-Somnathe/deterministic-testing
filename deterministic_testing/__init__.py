"""Deterministic Testing -- deterministic simulation testing.

Every engineer has a bug that happens once a week in production and never in
testing. This turns it into a seed number.

The non-determinism that makes such bugs unreproducible does not come from your
program. It comes from outside it: the OS scheduler, the clock, the network.
Replace all three with things you control, drive them from a single integer, and
the program becomes a pure function of that integer. Every bug it can express then
has a permanent address.
"""

from __future__ import annotations

from .clock import MICROS_PER_SECOND, VirtualClock, to_micros, to_seconds
from .errors import (
    DeterministicTestingError,
    Deadlock,
    DeterminismViolation,
    InvariantViolation,
    NonDeterminismLeak,
    StepLimitExceeded,
)
from .guards import DeterminismGuard
from .network import Fault, FaultConfig, FaultKind, FaultPlan, Network
from .ops import Log, Message, Now, Op, ProcessGen, Random, Recv, Send, Sleep, Spawn, Yield
from .rng import DeterministicRandom, derive_seed
from .simulation import Invariant, RunResult, RunStatus, Simulation
from .trace import Event, Trace, canonical_json

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # core
    "Simulation",
    "RunResult",
    "RunStatus",
    "Invariant",
    # operations a process may yield
    "Send",
    "Recv",
    "Sleep",
    "Now",
    "Random",
    "Yield",
    "Log",
    "Spawn",
    "Op",
    "Message",
    "ProcessGen",
    # network and faults
    "Network",
    "FaultConfig",
    "FaultPlan",
    "Fault",
    "FaultKind",
    # time
    "VirtualClock",
    "to_micros",
    "to_seconds",
    "MICROS_PER_SECOND",
    # randomness
    "DeterministicRandom",
    "derive_seed",
    # trace
    "Trace",
    "Event",
    "canonical_json",
    # errors
    "DeterministicTestingError",
    "InvariantViolation",
    "Deadlock",
    "StepLimitExceeded",
    "DeterminismViolation",
    "NonDeterminismLeak",
    "DeterminismGuard",
]
