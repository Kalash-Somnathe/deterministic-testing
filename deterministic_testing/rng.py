"""Seeded randomness.

Why this module exists at all
-----------------------------
The whole framework rests on a single invariant: *every* decision that could have
gone another way is derived from one integer seed. If even one decision escapes
(an unseeded ``random.random()``, a ``set`` iteration order, a wall-clock read)
then a reported seed does not reproduce, and the tool is worse than useless --
it is actively misleading.

So randomness is not "a call to the random module". It is an owned, named,
auditable resource. This module is the only place in `deterministic_testing` that is allowed to
touch :mod:`random`.

Two deliberate design choices, both of which cost a little code and buy a lot:

1. **We derive integers and floats ourselves from ``getrandbits``.**
   CPython guarantees the *bit stream* of the Mersenne Twister is stable across
   versions, but it does **not** guarantee that ``randrange``/``choice`` will keep
   consuming that stream in the same way forever (``randrange`` has already
   changed behaviour once, in 3.10/3.11, around non-integer arguments). If we
   called ``choice`` and CPython changed how many bits it consumes, every stored
   seed in every bug report would silently start replaying a different scenario.
   By implementing ``below()`` on top of ``getrandbits`` here, the mapping from
   bit stream to decision is pinned in our repository, under our tests.

2. **Named, independent sub-streams.**
   Scheduling decisions, fault decisions and application-level randomness draw
   from separate generators derived from the same root seed. This means adding a
   fault-injection decision does not shift every subsequent scheduling decision.
   Without it, changing one knob reshuffles the entire run and shrinking becomes
   far noisier than it needs to be.
"""

from __future__ import annotations

import hashlib
import random
from typing import Sequence, TypeVar

T = TypeVar("T")

__all__ = ["DeterministicRandom", "derive_seed"]


def derive_seed(root_seed: int, stream_name: str) -> int:
    """Derive a stable child seed for a named sub-stream.

    Uses SHA-256 rather than Python's ``hash()`` on purpose. ``hash()`` of a
    ``str`` is salted per-process by ``PYTHONHASHSEED``, so using it here would
    make sub-stream seeds differ between runs on the *same* machine. That is the
    exact failure mode this project is built to expose, and we would have shipped
    it in the foundations. SHA-256 is a pure function of its bytes, forever.
    """
    digest = hashlib.sha256(f"{root_seed}:{stream_name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


class DeterministicRandom:
    """A seeded random source with a pinned, version-stable derivation.

    Every method records how many raw bits it consumed, which makes it possible
    to assert in tests that two runs consumed the generator identically -- a
    cheap and surprisingly effective canary for accidental non-determinism.
    """

    __slots__ = ("_rng", "_seed", "_name", "_draws")

    def __init__(self, seed: int, name: str = "root") -> None:
        self._seed = int(seed)
        self._name = name
        self._rng = random.Random(self._seed)
        self._draws = 0

    # -- introspection ---------------------------------------------------

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def name(self) -> str:
        return self._name

    @property
    def draws(self) -> int:
        """Number of primitive draws taken. Used by tests as a determinism canary."""
        return self._draws

    def substream(self, name: str) -> "DeterministicRandom":
        """An independent generator derived from this one's seed and ``name``.

        Independent means: consuming from the child never advances the parent.
        That decoupling is what lets us add a new source of randomness to one
        subsystem without invalidating every previously-recorded seed for the
        others.
        """
        return DeterministicRandom(derive_seed(self._seed, name), f"{self._name}.{name}")

    # -- primitives ------------------------------------------------------

    def below(self, n: int) -> int:
        """Uniform integer in ``[0, n)``.

        Rejection sampling on the raw bit stream. This is the same algorithm
        CPython uses internally, reimplemented here so that the bit-stream ->
        decision mapping is *ours* and cannot drift under us on a version bump.
        """
        if n <= 0:
            raise ValueError(f"below() requires n > 0, got {n}")
        if n == 1:
            self._draws += 1
            return 0
        k = (n - 1).bit_length()
        while True:
            self._draws += 1
            value = self._rng.getrandbits(k)
            if value < n:
                return value

    def unit(self) -> float:
        """Uniform float in ``[0, 1)`` with 53 bits of precision.

        Deliberately not ``random.random()``: that consumes the stream in a
        CPython-internal pattern (two 32-bit words combined by ``genrand_res53``).
        ``getrandbits(53)`` is a documented, stable primitive.
        """
        self._draws += 1
        return self._rng.getrandbits(53) / float(1 << 53)

    def chance(self, probability: float) -> bool:
        """True with the given probability.

        Note the strict ``<``: ``chance(0.0)`` is always False and ``chance(1.0)``
        is always True, because ``unit()`` is in ``[0, 1)``. Tests rely on this.
        """
        if probability <= 0.0:
            return False
        if probability >= 1.0:
            return True
        return self.unit() < probability

    def choice(self, items: Sequence[T]) -> T:
        """Pick one element of a **sequence**.

        The type annotation says ``Sequence`` and it is enforced at runtime. A
        ``set`` has no defined iteration order across processes with hash
        randomisation enabled, so allowing one here would inject exactly the
        non-determinism this framework hunts. Callers must sort first.
        """
        if isinstance(items, (set, frozenset, dict)):
            raise TypeError(
                "choice() refuses unordered containers (set/frozenset/dict). "
                "Their iteration order is not guaranteed stable across processes, "
                "which would break replay. Pass a sorted list."
            )
        if not items:
            raise ValueError("choice() from an empty sequence")
        return items[self.below(len(items))]

    def integer(self, low: int, high: int) -> int:
        """Uniform integer in the inclusive range ``[low, high]``."""
        if high < low:
            raise ValueError(f"integer() requires low <= high, got [{low}, {high}]")
        return low + self.below(high - low + 1)

    def shuffled(self, items: Sequence[T]) -> list[T]:
        """A shuffled copy, via an explicit Fisher-Yates on our own ``below()``.

        We do not call ``random.shuffle`` for the same reason we do not call
        ``randrange``: its consumption pattern is an implementation detail.
        """
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DeterministicRandom(seed={self._seed}, name={self._name!r}, draws={self._draws})"
