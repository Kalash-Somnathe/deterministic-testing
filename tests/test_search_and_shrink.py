"""Tests for the seed search and the shrinker.

Shrinking is the feature that makes a bug report readable, so "it produced
something smaller" is not enough -- the smaller thing has to still reproduce the
*same* bug, and it has to be reproducible in its own right.
"""

from __future__ import annotations

import json

import pytest

from deterministic_testing import FaultConfig, FaultPlan, RunStatus
from deterministic_testing.search import Scenario, ddmin, search, shrink
from examples.pipeline import make_runner

BUGGY = make_runner(FaultConfig.realistic(), variant="buggy", default_items=4)
FIXED = make_runner(FaultConfig.realistic(), variant="fixed", default_items=4)


# -- search --------------------------------------------------------------


def test_search_finds_the_known_failing_seed() -> None:
    report = search(BUGGY, range(0, 50), workload=4, stop_on_first=True)
    assert report.failures
    assert report.failures[0][0] == 1


def test_search_over_the_fixed_pipeline_finds_nothing() -> None:
    report = search(FIXED, range(0, 500), workload=4, stop_on_first=False)
    assert report.failures == []
    assert report.statuses == {RunStatus.COMPLETED: 500}


def test_search_is_resumable(tmp_path) -> None:
    """A search over a hundred thousand seeds that dies at 60,000 must resume at
    60,000, not start again."""
    checkpoint = tmp_path / "search.jsonl"

    first = search(BUGGY, range(0, 20), workload=4, stop_on_first=False, checkpoint=checkpoint)
    assert first.seeds_run == 20
    lines = checkpoint.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20
    assert {json.loads(line)["seed"] for line in lines} == set(range(20))

    # Re-running the same range must add nothing and still report the failures.
    second = search(BUGGY, range(0, 20), workload=4, stop_on_first=False, checkpoint=checkpoint)
    assert len(checkpoint.read_text(encoding="utf-8").strip().splitlines()) == 20
    assert second.seeds_run == 20
    assert {f[0] for f in second.failures} == {f[0] for f in first.failures}

    # Extending the range only runs the new seeds.
    third = search(BUGGY, range(0, 30), workload=4, stop_on_first=False, checkpoint=checkpoint)
    assert len(checkpoint.read_text(encoding="utf-8").strip().splitlines()) == 30
    assert third.seeds_run == 30


# -- ddmin ---------------------------------------------------------------


def test_ddmin_isolates_the_required_element() -> None:
    """Pure algorithm check, no simulation: only element 7 matters."""
    from deterministic_testing.network import Fault

    items = [Fault(i, "DROP") for i in range(20)]
    required = items[7]
    minimal = ddmin(items, lambda subset: required in subset)
    assert minimal == [required]


def test_ddmin_handles_a_pair_that_must_stay_together() -> None:
    from deterministic_testing.network import Fault

    items = [Fault(i, "DROP") for i in range(16)]
    needed = {items[3], items[11]}
    minimal = ddmin(items, lambda subset: needed <= set(subset))
    assert set(minimal) == needed


# -- shrink --------------------------------------------------------------


def test_shrink_reduces_the_scenario_and_still_reproduces() -> None:
    report = shrink(BUGGY, Scenario(seed=1, workload=4))

    assert report.minimal_faults < report.original_faults
    assert report.minimal_events < report.original_events
    assert report.invariant == "each item credited at most once"

    # The minimal scenario must reproduce on its own, from its own description.
    replayed = BUGGY(report.minimal)
    assert replayed.violated
    assert replayed.violation is not None
    assert replayed.violation.name == report.invariant


def test_shrink_result_is_itself_deterministic() -> None:
    """Shrinking is a search. A search that returns different answers each time
    would make the minimal scenario unciteable in a bug report."""
    first = shrink(BUGGY, Scenario(seed=1, workload=4))
    second = shrink(BUGGY, Scenario(seed=1, workload=4))
    assert first.minimal == second.minimal
    assert first.result.digest() == second.result.digest()


def test_shrunk_scenario_replays_identically_many_times() -> None:
    report = shrink(BUGGY, Scenario(seed=1, workload=4))
    digests = {BUGGY(report.minimal).digest() for _ in range(30)}
    assert len(digests) == 1


def test_shrink_refuses_a_scenario_that_does_not_fail() -> None:
    with pytest.raises(ValueError, match="violates an invariant"):
        shrink(BUGGY, Scenario(seed=0, workload=4))


def test_plan_mode_uses_no_fault_randomness() -> None:
    """Replaying an explicit plan must inject exactly those faults and no others.

    This is what makes a shrunk scenario a *description* rather than a hint.
    """
    baseline = BUGGY(Scenario(seed=1, workload=4))
    plan = baseline.fault_plan
    assert len(plan) > 0

    replayed = BUGGY(Scenario(seed=1, workload=4, plan=plan))
    assert replayed.digest() == baseline.digest()

    empty = BUGGY(Scenario(seed=1, workload=4, plan=FaultPlan()))
    assert len(empty.fault_plan) == 0
    assert empty.digest() != baseline.digest()


def test_fault_plan_round_trips_through_json() -> None:
    baseline = BUGGY(Scenario(seed=1, workload=4))
    restored = FaultPlan.from_json(json.loads(json.dumps(baseline.fault_plan.to_json())))
    assert restored == baseline.fault_plan
    assert BUGGY(Scenario(seed=1, workload=4, plan=restored)).digest() == baseline.digest()


def test_scenario_round_trips_through_json() -> None:
    report = shrink(BUGGY, Scenario(seed=1, workload=4))
    restored = Scenario.from_json(json.loads(json.dumps(report.minimal.to_json())))
    assert restored == report.minimal
    assert BUGGY(restored).violated
