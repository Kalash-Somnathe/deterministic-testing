"""Tests of the system under test itself.

The first class of test here is the point of the whole project: it is a
*conventional* test suite. It is not lazy or strawman -- it runs the pipeline a
thousand times, checks the totals, checks idempotency, and passes. It passes
because it exercises the code on a network that never misbehaves, which is the
only network most test suites have ever seen.

The second class is what `deterministic_testing` adds.
"""

from __future__ import annotations

import pytest

from deterministic_testing import FaultConfig, RunStatus, Simulation
from deterministic_testing.search import Scenario, search
from examples.pipeline import PipelineConfig, build_pipeline, expected_keys, make_runner

VARIANTS = ["buggy", "claim", "fixed"]


def run_pipeline(seed: int, *, items: int = 4, variant: str = "buggy", faults: FaultConfig | None = None):
    simulation = Simulation(
        seed,
        fault_config=faults if faults is not None else FaultConfig.none(),
        max_steps=200_000,
    )
    state = build_pipeline(simulation, PipelineConfig(item_count=items, variant=variant))
    return simulation.run(), state


# -- the conventional suite: green, and wrong --------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
def test_pipeline_processes_every_item_exactly_once(variant: str) -> None:
    result, state = run_pipeline(0, variant=variant)
    assert result.status == RunStatus.COMPLETED
    assert state.total == 4
    assert sorted(state.credits) == expected_keys(PipelineConfig(item_count=4))
    assert len(state.credits) == len(set(state.credits))


def test_buggy_pipeline_passes_a_thousand_consecutive_runs() -> None:
    """One thousand runs of the buggy pipeline on a healthy network.

    Every one passes. This is the test suite that ships the bug. Nothing about it
    is unreasonable: it varies the seed, so it varies the interleaving; it checks
    the right property. What it cannot do is make the network misbehave, and the
    bug needs a dropped acknowledgement to appear.
    """
    for seed in range(1000):
        result, state = run_pipeline(seed, variant="buggy")
        assert result.status == RunStatus.COMPLETED, f"seed {seed}: {result.status}"
        assert state.total == 4
        assert len(state.credits) == len(set(state.credits))


@pytest.mark.parametrize("items", [1, 2, 4, 8])
@pytest.mark.parametrize("variant", VARIANTS)
def test_pipeline_handles_various_workloads_on_a_healthy_network(items: int, variant: str) -> None:
    result, state = run_pipeline(3, items=items, variant=variant)
    assert result.status == RunStatus.COMPLETED
    assert state.total == items


# -- what deterministic_testing adds --------------------------------------


def test_deterministic_testing_finds_the_planted_bug() -> None:
    """The claim this project makes, as an executable assertion."""
    result, state = run_pipeline(1, variant="buggy", faults=FaultConfig.realistic())
    assert result.status == RunStatus.VIOLATION
    assert result.violation is not None
    assert result.violation.name == "each item credited at most once"
    assert state.credits == ["item-0", "item-0"]
    assert state.total == 2


def test_deterministic_testing_finds_the_unplanted_bug_in_the_first_fix() -> None:
    """Seed 11 against the atomic-claim consumer.

    The claim removes the check-then-act window and does fix the planted bug. It
    does not fix the pipeline, because replies are correlated by (op, key) and a
    duplicated reply to an earlier request is indistinguishable from the reply to
    this one. Nobody planted this; the search found it.
    """
    result, _ = run_pipeline(11, variant="claim", faults=FaultConfig.realistic())
    assert result.status == RunStatus.VIOLATION
    assert result.violation is not None
    assert result.violation.name == "each item credited at most once"


def test_the_first_fix_is_better_but_still_broken() -> None:
    """A fix that makes a bug seven times rarer looks like a fix in production."""
    buggy = search(
        make_runner(FaultConfig.realistic(), variant="buggy", default_items=4),
        range(0, 300),
        workload=4,
        stop_on_first=False,
    )
    claim = search(
        make_runner(FaultConfig.realistic(), variant="claim", default_items=4),
        range(0, 300),
        workload=4,
        stop_on_first=False,
    )
    assert len(claim.failures) < len(buggy.failures) / 3
    assert len(claim.failures) > 0


def test_the_real_fix_survives_a_large_seed_search() -> None:
    """The acceptance criterion for the fix: not 'looks right', but 'two thousand
    adversarial interleavings and fault schedules could not break it'."""
    report = search(
        make_runner(FaultConfig.realistic(), variant="fixed", default_items=4),
        range(0, 2000),
        workload=4,
        stop_on_first=False,
    )
    assert report.failures == []
    assert report.statuses == {RunStatus.COMPLETED: 2000}


@pytest.mark.parametrize("items", [1, 2, 3, 6])
def test_the_real_fix_holds_across_workload_sizes(items: int) -> None:
    report = search(
        make_runner(FaultConfig.realistic(), variant="fixed", default_items=items),
        range(0, 300),
        workload=items,
        stop_on_first=False,
    )
    assert report.failures == []


def test_no_run_ever_deadlocks_or_hits_the_step_limit() -> None:
    """Distinguish 'the invariant held' from 'the run fell over before it could
    be violated'. A search reporting clean seeds because every run deadlocked
    would be worthless, and the distinction is easy to lose."""
    runner = make_runner(FaultConfig.realistic(), variant="fixed", default_items=4)
    statuses = {runner(Scenario(seed=seed, workload=4)).status for seed in range(500)}
    assert statuses == {RunStatus.COMPLETED}


def test_unknown_variant_is_rejected() -> None:
    simulation = Simulation(1)
    with pytest.raises(ValueError, match="unknown worker variant"):
        build_pipeline(simulation, PipelineConfig(variant="nonsense"))
