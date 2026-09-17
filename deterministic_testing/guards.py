"""Runtime enforcement of the one invariant everything else depends on.

The framework's promise is: *the run is a pure function of the seed*. That promise
is only as strong as the weakest line of code in the system under test. One
forgotten ``time.time()`` in a retry-backoff calculation, one ``random.random()``
for jitter, and a reported seed quietly stops reproducing -- while still looking
like it works, which is worse.

A convention in a README does not prevent that. So during a run we replace the
real sources of non-determinism with functions that raise. If the system under
test reaches for the wall clock, it fails immediately and loudly, at the exact
line, instead of producing a bug report nobody can reproduce three weeks later.

Scope and honesty about what this does *not* cover:

* It is a Python-level patch of module attributes. Code holding a pre-bound
  reference (``from time import time`` executed before the guard installed) slips
  through, as does anything in a C extension. It raises the cost of a mistake from
  zero to high; it is not a sandbox.
* It is process-global for the duration of the run and restored in a ``finally``.
  It is therefore not safe under real threads -- which is fine, because using real
  threads is exactly what this framework tells you not to do.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .errors import NonDeterminismLeak

__all__ = ["DeterminismGuard", "GUARDED_TARGETS"]


GUARDED_TARGETS: tuple[tuple[Any, str, str], ...] = (
    # (module, attribute, human-readable replacement advice)
    (time, "time", "yield deterministic_testing.Now() to read the virtual clock"),
    (time, "time_ns", "yield deterministic_testing.Now() to read the virtual clock"),
    (time, "monotonic", "yield deterministic_testing.Now() to read the virtual clock"),
    (time, "monotonic_ns", "yield deterministic_testing.Now() to read the virtual clock"),
    (time, "perf_counter", "yield deterministic_testing.Now() to read the virtual clock"),
    (time, "perf_counter_ns", "yield deterministic_testing.Now() to read the virtual clock"),
    (time, "sleep", "yield deterministic_testing.Sleep(seconds) to advance the virtual clock"),
    (random, "random", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (random, "randint", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (random, "randrange", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (random, "uniform", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (random, "choice", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (random, "shuffle", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (random, "sample", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (random, "getrandbits", "yield deterministic_testing.Random() to draw from the seeded stream"),
    (uuid, "uuid4", "derive ids from a counter or from deterministic_testing.Random()"),
    (os, "urandom", "derive ids from a counter or from deterministic_testing.Random()"),
)


@dataclass(slots=True)
class DeterminismGuard:
    """Context manager that makes real non-determinism raise.

    ``violations`` accumulates every attempt even when ``strict`` is False, which
    makes it usable as an audit tool on a codebase you are not ready to change
    yet: run once permissively, read the list, then turn strict on.
    """

    strict: bool = True
    violations: list[str] = field(default_factory=list)
    _saved: list[tuple[Any, str, Any]] = field(default_factory=list, repr=False)

    def _make_stub(self, module: Any, name: str, advice: str) -> Callable[..., Any]:
        qualified = f"{module.__name__}.{name}()"

        def stub(*_args: Any, **_kwargs: Any) -> Any:
            message = (
                f"{qualified} was called inside a deterministic_testing simulation. "
                f"Every source of non-determinism must route through the seeded "
                f"Simulation, otherwise the reported seed does not reproduce the run. "
                f"Instead: {advice}."
            )
            self.violations.append(qualified)
            if self.strict:
                raise NonDeterminismLeak(message)
            return None

        stub.__name__ = f"deterministic_testing_guard_{name}"
        stub.__doc__ = f"Guarded replacement for {qualified}."
        return stub

    def __enter__(self) -> "DeterminismGuard":
        for module, name, advice in GUARDED_TARGETS:
            if not hasattr(module, name):  # pragma: no cover - version differences
                continue
            self._saved.append((module, name, getattr(module, name)))
            setattr(module, name, self._make_stub(module, name, advice))
        return self

    def __exit__(self, *_exc: Any) -> None:
        # Restore in reverse so that nesting (which should not happen, but might
        # in a test that exercises the guard) unwinds correctly.
        for module, name, original in reversed(self._saved):
            setattr(module, name, original)
        self._saved.clear()

    def guarded_names(self) -> Iterable[str]:
        return (f"{m.__name__}.{n}" for m, n, _ in GUARDED_TARGETS)
