"""Unit tests for the framework's own machinery.

Each of these guards a specific way the determinism guarantee could rot: the RNG
drifting between Python versions, the clock going backwards, a trace digest that
depends on dict insertion order, a scheduler that never actually interleaves.
"""

from __future__ import annotations

import random
import time

import pytest

from deterministic_testing import (
    Deadlock,
    DeterminismGuard,
    DeterministicRandom,
    Event,
    FaultConfig,
    Log,
    NonDeterminismLeak,
    Now,
    Random,
    Recv,
    RunStatus,
    Send,
    Simulation,
    Sleep,
    Trace,
    VirtualClock,
    Yield,
    canonical_json,
    derive_seed,
    to_micros,
    to_seconds,
)


# -- randomness ----------------------------------------------------------


def test_same_seed_same_sequence() -> None:
    a = DeterministicRandom(99)
    b = DeterministicRandom(99)
    assert [a.below(1000) for _ in range(200)] == [b.below(1000) for _ in range(200)]


def test_substreams_are_independent() -> None:
    """Drawing from a child must not advance the parent.

    This is what lets us add a fault decision without shifting every subsequent
    scheduling decision.
    """
    parent = DeterministicRandom(5)
    before = [parent.below(100) for _ in range(3)]

    parent2 = DeterministicRandom(5)
    child = parent2.substream("fault")
    [child.below(100) for _ in range(50)]
    after = [parent2.below(100) for _ in range(3)]

    assert before == after


def test_substream_seeds_are_not_process_dependent() -> None:
    """Derived seeds come from SHA-256, never from ``hash()``.

    ``hash("fault")`` differs between processes. If sub-stream seeds used it, two
    runs of the same seed on the same machine would diverge.
    """
    assert derive_seed(1234, "schedule") == derive_seed(1234, "schedule")
    assert derive_seed(1234, "schedule") != derive_seed(1234, "fault")
    # A pinned value: if this ever changes, every recorded seed is invalidated,
    # and that should be a deliberate, visible decision rather than a surprise.
    assert derive_seed(0, "schedule") == 7824849348043763595


def test_below_is_uniform_enough_and_in_range() -> None:
    rng = DeterministicRandom(3)
    counts = [0] * 6
    for _ in range(60_000):
        value = rng.below(6)
        assert 0 <= value < 6
        counts[value] += 1
    assert all(8_000 < count < 12_000 for count in counts), counts


def test_below_rejects_bad_bounds() -> None:
    rng = DeterministicRandom(1)
    with pytest.raises(ValueError):
        rng.below(0)


def test_choice_refuses_unordered_containers() -> None:
    """A set's iteration order is not stable across processes. Passing one to a
    seeded chooser is the single easiest way to silently break replay, so it is a
    hard error rather than a lint rule."""
    rng = DeterministicRandom(1)
    with pytest.raises(TypeError, match="unordered"):
        rng.choice({"a", "b", "c"})  # type: ignore[arg-type]
    assert rng.choice(["a", "b", "c"]) in ("a", "b", "c")


def test_chance_boundaries() -> None:
    rng = DeterministicRandom(1)
    assert rng.chance(0.0) is False
    assert rng.chance(1.0) is True


def test_shuffled_is_a_permutation_and_reproducible() -> None:
    items = list(range(20))
    first = DeterministicRandom(8).shuffled(items)
    second = DeterministicRandom(8).shuffled(items)
    assert first == second
    assert sorted(first) == items
    assert first != items


# -- clock ---------------------------------------------------------------


def test_clock_starts_at_zero_and_advances() -> None:
    clock = VirtualClock()
    assert clock.now == 0
    assert clock.advance_to(1_500_000) == 1_500_000
    assert clock.now_seconds == 1.5


def test_clock_refuses_to_go_backwards() -> None:
    clock = VirtualClock()
    clock.advance_to(100)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(99)


def test_micros_conversion_is_exact_for_repeated_addition() -> None:
    """The reason time is an integer: 0.1 added ten times is not 1.0 in floats."""
    total = 0
    for _ in range(10):
        total += to_micros(0.1)
    assert total == to_micros(1.0)
    assert to_seconds(total) == 1.0


def test_virtual_time_costs_no_wall_time() -> None:
    """A one-hour sleep must not take an hour, or indeed any measurable time."""

    def sleeper():
        yield Sleep(3600.0)
        yield Log("woke")

    simulation = Simulation(1)
    simulation.spawn("sleeper", sleeper())
    started = time.perf_counter()
    result = simulation.run()
    elapsed = time.perf_counter() - started

    assert result.end_time_micros == to_micros(3600.0)
    assert elapsed < 1.0


# -- trace ---------------------------------------------------------------


def test_trace_digest_ignores_dict_insertion_order() -> None:
    """Two runs that did the same thing must hash the same, even if a payload's
    keys were built in a different order."""
    one = Trace([Event(1, 0, "SEND", "p", {"a": 1, "b": 2})])
    two = Trace([Event(1, 0, "SEND", "p", {"b": 2, "a": 1})])
    assert one.digest() == two.digest()


def test_trace_digest_is_sensitive_to_everything_that_matters() -> None:
    base = Event(1, 0, "SEND", "p", {"a": 1})
    reference = Trace([base]).digest()
    assert Trace([Event(2, 0, "SEND", "p", {"a": 1})]).digest() != reference
    assert Trace([Event(1, 1, "SEND", "p", {"a": 1})]).digest() != reference
    assert Trace([Event(1, 0, "RECV", "p", {"a": 1})]).digest() != reference
    assert Trace([Event(1, 0, "SEND", "q", {"a": 1})]).digest() != reference
    assert Trace([Event(1, 0, "SEND", "p", {"a": 2})]).digest() != reference


def test_trace_digest_is_order_sensitive() -> None:
    """Interleaving is the whole subject. Two traces with the same events in a
    different order describe different runs and must not collide."""
    a = Event(1, 0, "SEND", "p", {})
    b = Event(2, 0, "SEND", "q", {})
    assert Trace([a, b]).digest() != Trace([b, a]).digest()


def test_canonical_json_survives_unserialisable_values() -> None:
    assert canonical_json({"x": object()}).startswith('{"x":')


# -- scheduler -----------------------------------------------------------


def test_processes_run_to_completion() -> None:
    log: list[str] = []

    def counter(name: str, n: int):
        for i in range(n):
            log.append(f"{name}{i}")
            yield Yield()

    simulation = Simulation(1)
    simulation.spawn("a", counter("a", 3))
    simulation.spawn("b", counter("b", 3))
    result = simulation.run()

    assert result.status == RunStatus.COMPLETED
    assert sorted(log) == ["a0", "a1", "a2", "b0", "b1", "b2"]


def test_scheduler_actually_interleaves() -> None:
    """If the scheduler ran processes to completion one at a time it would be
    deterministic, fast, and completely useless."""
    orders = set()
    for seed in range(40):
        log: list[str] = []

        def counter(name: str, n: int = 4):
            for i in range(n):
                log.append(f"{name}{i}")
                yield Yield()

        simulation = Simulation(seed)
        simulation.spawn("a", counter("a"))
        simulation.spawn("b", counter("b"))
        simulation.run()
        orders.add(tuple(log))
    assert len(orders) > 5, f"only {len(orders)} distinct interleavings from 40 seeds"


def test_now_returns_virtual_time() -> None:
    seen: list[int] = []

    def reader():
        seen.append((yield Now()))
        yield Sleep(2.5)
        seen.append((yield Now()))

    simulation = Simulation(1)
    simulation.spawn("reader", reader())
    simulation.run()
    assert seen == [0, to_micros(2.5)]


def test_random_op_draws_from_the_seeded_stream() -> None:
    def drawer(out: list[float]):
        for _ in range(5):
            out.append((yield Random()))

    first: list[float] = []
    simulation = Simulation(9)
    simulation.spawn("d", drawer(first))
    simulation.run()

    second: list[float] = []
    simulation2 = Simulation(9)
    simulation2.spawn("d", drawer(second))
    simulation2.run()

    assert first == second
    assert all(0.0 <= value < 1.0 for value in first)


def test_recv_timeout_yields_none_and_costs_no_wall_time() -> None:
    outcome: list[object] = []

    def waiter():
        outcome.append((yield Recv(timeout=3600.0)))

    simulation = Simulation(1)
    simulation.spawn("waiter", waiter())
    result = simulation.run()

    assert outcome == [None]
    assert result.end_time_micros == to_micros(3600.0)


def test_deadlock_is_detected_not_hung() -> None:
    """An unbounded wait for a message nobody will send is a real bug in the
    system under test, and must be reported as one rather than hanging the run."""

    def waiter():
        yield Recv()  # no timeout, no sender

    simulation = Simulation(1)
    simulation.spawn("waiter", waiter())
    result = simulation.run()

    assert result.status == RunStatus.DEADLOCK
    assert isinstance(result.error, Deadlock)


def test_step_limit_is_an_error_not_a_silent_truncation() -> None:
    """A truncated run could hide a violation two steps away and report a clean
    seed. That is the one outcome worse than a false alarm."""

    def spinner():
        while True:
            yield Yield()

    simulation = Simulation(1, max_steps=50)
    simulation.spawn("spin", spinner())
    result = simulation.run()
    assert result.status == RunStatus.STEP_LIMIT


def test_selective_receive_leaves_non_matching_messages_alone() -> None:
    received: list[str] = []

    def sender():
        yield Send("picky", {"op": "junk"})
        yield Send("picky", {"op": "wanted"})

    def picky():
        first = yield Recv(timeout=5.0, match=lambda m: m.payload["op"] == "wanted")
        received.append(first.payload["op"])
        second = yield Recv(timeout=5.0)
        received.append(second.payload["op"])

    simulation = Simulation(1)
    simulation.spawn("sender", sender())
    simulation.spawn("picky", picky())
    simulation.run()

    assert received == ["wanted", "junk"]


def test_simulation_is_single_use() -> None:
    simulation = Simulation(1)
    simulation.run()
    with pytest.raises(RuntimeError, match="single-use"):
        simulation.run()


def test_yielding_a_non_op_is_a_clear_error() -> None:
    def confused():
        yield "not an op"

    simulation = Simulation(1)
    simulation.spawn("c", confused())
    result = simulation.run()
    assert result.status == RunStatus.ERROR
    assert isinstance(result.error, TypeError)


# -- invariants ----------------------------------------------------------


def test_invariant_is_checked_after_every_step() -> None:
    counter = {"n": 0}

    def incrementer():
        for _ in range(10):
            counter["n"] += 1
            yield Yield()

    simulation = Simulation(1)
    simulation.spawn("inc", incrementer())
    simulation.invariant("n stays below 4", lambda: counter["n"] < 4)
    result = simulation.run()

    assert result.status == RunStatus.VIOLATION
    assert result.violation is not None
    assert result.violation.name == "n stays below 4"
    # Caught at the moment of breach, not at the end of the run.
    assert counter["n"] == 4


def test_invariant_that_raises_is_reported_as_a_violation() -> None:
    simulation = Simulation(1)
    simulation.spawn("noop", (lambda: (yield Yield()))())
    simulation.invariant("explodes", lambda: 1 // 0 == 0)
    result = simulation.run()
    assert result.status == RunStatus.VIOLATION
    assert "ZeroDivisionError" in (result.violation.detail if result.violation else "")


def test_violation_carries_the_seed_and_the_trace() -> None:
    simulation = Simulation(4242)
    simulation.spawn("noop", (lambda: (yield Yield()))())
    simulation.invariant("never", lambda: False)
    result = simulation.run()
    assert result.violation is not None
    assert result.violation.seed == 4242
    assert result.violation.trace is not None
    assert len(result.violation.trace) > 0


# -- the determinism guard -----------------------------------------------


def test_guard_blocks_wall_clock_and_unseeded_random() -> None:
    with DeterminismGuard(strict=True):
        with pytest.raises(NonDeterminismLeak, match="time.time"):
            time.time()
        with pytest.raises(NonDeterminismLeak, match="random.random"):
            random.random()
    # ...and puts everything back.
    assert isinstance(time.time(), float)
    assert 0.0 <= random.random() < 1.0


def test_guard_is_active_during_a_run() -> None:
    def cheater():
        time.time()
        yield Yield()

    simulation = Simulation(1)
    simulation.spawn("cheater", cheater())
    result = simulation.run()
    assert result.status == RunStatus.ERROR
    assert isinstance(result.error, NonDeterminismLeak)


def test_guard_can_audit_without_raising() -> None:
    guard = DeterminismGuard(strict=False)
    with guard:
        time.time()
        random.random()
    assert guard.violations == ["time.time()", "random.random()"]


def test_seeded_generator_still_works_under_the_guard() -> None:
    """The guard patches module-level functions. Our own ``random.Random``
    instances are untouched, which is the whole point: seeded randomness stays
    available while unseeded randomness does not."""
    with DeterminismGuard(strict=True):
        rng = DeterministicRandom(1)
        assert 0 <= rng.below(10) < 10


# -- faults --------------------------------------------------------------


def test_no_faults_means_no_faults() -> None:
    def pinger():
        for _ in range(20):
            yield Send("sink", {"x": 1})

    def sink():
        while True:
            message = yield Recv(timeout=1.0)
            if message is None:
                return

    simulation = Simulation(1, fault_config=FaultConfig.none())
    simulation.spawn("pinger", pinger())
    simulation.spawn("sink", sink())
    result = simulation.run()
    assert len(result.fault_plan) == 0


def test_faults_fire_and_are_recorded() -> None:
    def pinger():
        for _ in range(60):
            yield Send("sink", {"x": 1})

    def sink():
        while True:
            message = yield Recv(timeout=1.0)
            if message is None:
                return

    simulation = Simulation(3, fault_config=FaultConfig.realistic())
    simulation.spawn("pinger", pinger())
    simulation.spawn("sink", sink())
    result = simulation.run()
    assert len(result.fault_plan) > 0
    assert {f.kind for f in result.fault_plan} <= {"DROP", "DUPLICATE", "DELAY"}
