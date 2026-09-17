"""The determinism proof.

This is the most important file in the repository. Everything `deterministic_testing` claims rests
on one property: the same seed produces the same run, byte for byte, always. If
that property does not hold, every seed the tool has ever printed is a lie, and a
tool that hands you unreproducible reproduction instructions is worse than no tool
at all.

So the property is not asserted in prose. It is executed a hundred times, across
several fault profiles, across several workload sizes, and -- the part that
actually catches real mistakes -- in separate operating-system processes with
different values of ``PYTHONHASHSEED``.

That last one matters because the most likely way to break determinism in Python
is not exotic. It is iterating a ``set``, or calling ``hash()`` on a string.
Neither shows up in a single-process test loop, because within one process the
hash salt is fixed. Only a subprocess with a different salt exposes it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from deterministic_testing import FaultConfig, Simulation
from deterministic_testing.search import Scenario
from examples.pipeline import PipelineConfig, build_pipeline, make_runner

REPO_ROOT = Path(__file__).resolve().parent.parent

PROFILES = {
    "none": FaultConfig.none(),
    "realistic": FaultConfig.realistic(),
}


def run_once(seed: int, *, items: int = 4, profile: str = "realistic", variant: str = "buggy"):
    simulation = Simulation(seed, fault_config=PROFILES[profile], max_steps=200_000)
    build_pipeline(simulation, PipelineConfig(item_count=items, variant=variant))
    return simulation.run()


# -- the headline test ---------------------------------------------------


@pytest.mark.parametrize("seed", [0, 1, 11, 12345])
def test_same_seed_gives_byte_identical_trace_100_times(seed: int) -> None:
    """Run one seed a hundred times. Every trace digest must be the same one.

    Not "the same length", not "the same outcome" -- the same SHA-256 of the full
    ordered event log, including every virtual timestamp and every payload.
    """
    digests = {run_once(seed).digest() for _ in range(100)}
    assert len(digests) == 1, (
        f"seed {seed} produced {len(digests)} distinct traces across 100 runs. "
        f"Determinism is broken; every seed this tool reports is void."
    )


def test_determinism_holds_across_fault_profiles_and_workloads() -> None:
    """The guarantee must not be an accident of one configuration."""
    for profile in sorted(PROFILES):
        for items in (1, 2, 4, 8):
            for variant in ("buggy", "claim", "fixed"):
                digests = {
                    run_once(7, items=items, profile=profile, variant=variant).digest()
                    for _ in range(5)
                }
                assert len(digests) == 1, f"{profile}/{items}/{variant} is non-deterministic"


def test_full_run_state_is_identical_not_just_the_digest() -> None:
    """Guard against a digest that is stable because it is not capturing enough.

    A trace hash proves nothing if the trace omits the interesting part. This
    compares the observable outcome as well: step count, virtual end time, the
    ordered credit log, and the recorded fault plan.
    """
    outcomes = []
    for _ in range(20):
        simulation = Simulation(11, fault_config=FaultConfig.realistic(), max_steps=200_000)
        state = build_pipeline(simulation, PipelineConfig(item_count=4, variant="claim"))
        result = simulation.run()
        outcomes.append(
            (
                result.status,
                result.steps,
                result.end_time_micros,
                tuple(state.credits),
                state.total,
                tuple((f.seq, f.kind, f.value) for f in result.fault_plan),
                result.digest(),
            )
        )
    assert len(set(outcomes)) == 1


# -- the cross-process test ----------------------------------------------

_SUBPROCESS_SCRIPT = """
import json, sys
sys.path.insert(0, sys.argv[1])
from deterministic_testing import FaultConfig, Simulation
from examples.pipeline import PipelineConfig, build_pipeline

out = {}
for seed in (0, 1, 11, 12345):
    sim = Simulation(seed, fault_config=FaultConfig.realistic(), max_steps=200000)
    state = build_pipeline(sim, PipelineConfig(item_count=4, variant="buggy"))
    result = sim.run()
    out[str(seed)] = [result.digest(), result.status, result.steps, list(state.credits)]
print(json.dumps(out, sort_keys=True))
"""


@pytest.mark.parametrize("hash_seed", ["0", "1", "424242", "random", "random"])
def test_identical_across_processes_with_different_hash_seeds(
    hash_seed: str, tmp_path: Path
) -> None:
    """The test that catches ``set`` iteration and ``hash()``.

    Python salts ``str`` hashing per process unless ``PYTHONHASHSEED`` is fixed.
    Any code path whose behaviour depends on that salt -- iterating a set of
    strings, ordering a dict by hash, using ``hash()`` as an identifier -- yields
    a different run here and nowhere else.

    Note what this test does *not* do: it does not set ``PYTHONHASHSEED=0`` to make
    itself pass. Pinning the hash seed would hide exactly the bug it exists to
    find.
    """
    script = tmp_path / "child.py"
    script.write_text(_SUBPROCESS_SCRIPT, encoding="utf-8")

    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = hash_seed

    def run_child() -> dict:
        completed = subprocess.run(
            [sys.executable, str(script), str(REPO_ROOT)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=300,
            check=True,
        )
        return json.loads(completed.stdout)

    reference = run_child()
    other = run_child()
    assert reference == other

    # And it must equal what this process computes, too.
    in_process = {
        str(seed): [
            run_once(seed).digest(),
            run_once(seed).status,
            run_once(seed).steps,
            list(_credits_for(seed)),
        ]
        for seed in (0, 1, 11, 12345)
    }
    assert reference == in_process


def _credits_for(seed: int) -> list[str]:
    simulation = Simulation(seed, fault_config=FaultConfig.realistic(), max_steps=200_000)
    state = build_pipeline(simulation, PipelineConfig(item_count=4, variant="buggy"))
    simulation.run()
    return state.credits


# -- the converse: different seeds must actually explore different things -


def test_different_seeds_produce_different_traces() -> None:
    """A deterministic tool that always does the same thing is a very reliable
    tool that finds nothing. Distinct seeds must genuinely diverge."""
    digests = {run_once(seed).digest() for seed in range(50)}
    assert len(digests) >= 40, (
        f"only {len(digests)} distinct traces from 50 seeds; the scheduler is not "
        f"exploring enough of the interleaving space to be useful"
    )


def test_different_seeds_reach_different_outcomes() -> None:
    """Beyond distinct traces: distinct *results*, so the search has real work."""
    statuses = {run_once(seed).status for seed in range(200)}
    assert len(statuses) > 1


# -- the search driver must be deterministic too --------------------------


def test_runner_is_pure_across_calls() -> None:
    """The shrinker compares thousands of candidate runs. If any state leaked
    between calls -- a module global, a shared store, a generator reused -- those
    comparisons would be meaningless."""
    runner = make_runner(FaultConfig.realistic(), variant="buggy", default_items=4)
    scenario = Scenario(seed=11, workload=4)
    first = runner(scenario)
    interference = [runner(Scenario(seed=s, workload=3)) for s in range(5)]
    assert interference  # keep the calls, they are the point
    second = runner(scenario)
    assert first.digest() == second.digest()
