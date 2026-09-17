"""Searching many seeds for a violation, then shrinking it to something readable.

Two jobs live here, and the second is the one that turns this from a curiosity
into a tool people would actually use.

**Search** runs N seeds and reports the ones that fail. Embarrassingly parallel and
utterly boring, except for one detail: it checkpoints to disk as it goes, so a
search over a hundred thousand seeds that dies at seed 60,000 resumes at 60,000.

**Shrink** is the interesting part. A failing run is typically hundreds of events
with a dozen injected faults, and nobody debugs that. Shrinking searches for the
smallest *scenario* that still reproduces the same violation: fewer items in the
workload, and fewer faults injected. The result is usually a handful of events --
short enough that a human reads it once and understands the bug.

The algorithm is delta debugging (Zeller's ddmin) over the recorded fault plan,
interleaved with a linear reduction of the workload size, repeated until a pass
makes no progress.

Two honesty notes that belong in the code rather than a footnote:

* Removing a fault changes how many messages the run produces, so sequence numbers
  after that point no longer name the same messages. Shrinking is therefore a
  search over *configurations*, each candidate genuinely re-run and re-checked.
  It reliably finds something smaller; it does not prove minimality.
* Every candidate is required to violate the **same named invariant**. Without
  that check, shrinking happily wanders into a different bug and reports a minimal
  reproduction of something you were not looking for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .network import Fault, FaultPlan
from .simulation import RunResult, RunStatus

__all__ = ["Scenario", "SearchReport", "ShrinkReport", "search", "shrink", "ddmin"]


@dataclass(frozen=True, slots=True)
class Scenario:
    """A complete, replayable description of one run.

    ``seed`` alone is enough for a plain replay. ``plan`` and ``workload`` exist so
    that a *shrunk* scenario is also replayable: it is no longer "seed 1234", it is
    "seed 1234 with these three faults and two items".
    """

    seed: int
    workload: Optional[int] = None
    """Size knob for the system under test, e.g. number of items produced."""
    plan: Optional[FaultPlan] = None
    """When set, the network replays exactly these faults and consults no RNG."""

    def describe(self) -> str:
        parts = [f"seed={self.seed}"]
        if self.workload is not None:
            parts.append(f"workload={self.workload}")
        parts.append(f"faults={len(self.plan) if self.plan is not None else 'sampled'}")
        return " ".join(parts)

    def to_json(self) -> dict:
        return {
            "seed": self.seed,
            "workload": self.workload,
            "plan": self.plan.to_json() if self.plan is not None else None,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Scenario":
        plan = data.get("plan")
        return cls(
            seed=int(data["seed"]),
            workload=data.get("workload"),
            plan=FaultPlan.from_json(plan) if plan else None,
        )


Runner = Callable[[Scenario], RunResult]
"""Builds and runs one simulation from a scenario. Supplied by the caller.

It must be a pure function of the scenario: fresh state, fresh generators, no
memory of previous calls. If it is not, every guarantee in this file evaporates.
"""


@dataclass(slots=True)
class SearchReport:
    seeds_run: int = 0
    failures: list[tuple[int, str, str]] = field(default_factory=list)
    """(seed, status, short description) for each failing seed."""
    first_failure: Optional[Scenario] = None
    statuses: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(self.statuses.items()))
        return f"{self.seeds_run} seeds run ({breakdown}); {len(self.failures)} failing"


@dataclass(slots=True)
class ShrinkReport:
    original: Scenario
    minimal: Scenario
    original_events: int
    minimal_events: int
    original_faults: int
    minimal_faults: int
    candidates_tried: int
    invariant: str
    result: RunResult

    def summary(self) -> str:
        return (
            f"shrank {self.original_events} events / {self.original_faults} faults "
            f"-> {self.minimal_events} events / {self.minimal_faults} faults "
            f"in {self.candidates_tried} candidate runs"
        )


# -- search --------------------------------------------------------------


def search(
    runner: Runner,
    seeds: Iterable[int],
    *,
    workload: Optional[int] = None,
    stop_on_first: bool = True,
    checkpoint: Optional[Path] = None,
    progress: Optional[Callable[[int, RunResult], None]] = None,
) -> SearchReport:
    """Run ``seeds`` looking for a failure.

    ``checkpoint`` makes the search resumable: each seed's outcome is appended as
    a JSON line immediately, and seeds already present in the file are skipped on
    a re-run. A long search that dies to a machine reboot costs one seed, not the
    whole run.
    """
    report = SearchReport()
    done: set[int] = set()
    handle = None

    if checkpoint is not None:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        if checkpoint.exists():
            for line in checkpoint.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:  # pragma: no cover - truncated last line
                    continue
                done.add(int(record["seed"]))
                report.seeds_run += 1
                report.statuses[record["status"]] = report.statuses.get(record["status"], 0) + 1
                if record["status"] != RunStatus.COMPLETED:
                    report.failures.append((record["seed"], record["status"], record.get("detail", "")))
                    if report.first_failure is None:
                        report.first_failure = Scenario(seed=record["seed"], workload=workload)
        handle = checkpoint.open("a", encoding="utf-8")

    try:
        for seed in seeds:
            if seed in done:
                continue
            result = runner(Scenario(seed=seed, workload=workload))
            report.seeds_run += 1
            report.statuses[result.status] = report.statuses.get(result.status, 0) + 1

            detail = ""
            if result.violation is not None:
                detail = f"{result.violation.name}: {result.violation.detail}"
            elif result.error is not None:
                detail = f"{type(result.error).__name__}: {result.error}"

            if handle is not None:
                handle.write(
                    json.dumps(
                        {
                            "seed": seed,
                            "status": result.status,
                            "events": len(result.trace),
                            "faults": len(result.fault_plan),
                            "digest": result.trace.short_digest(),
                            "detail": detail,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                handle.flush()

            if progress is not None:
                progress(seed, result)

            if result.failed:
                report.failures.append((seed, result.status, detail))
                if report.first_failure is None:
                    report.first_failure = Scenario(seed=seed, workload=workload)
                if stop_on_first:
                    break
    finally:
        if handle is not None:
            handle.close()

    return report


# -- shrink --------------------------------------------------------------


def ddmin(
    items: Sequence[Fault],
    still_fails: Callable[[Sequence[Fault]], bool],
) -> list[Fault]:
    """Zeller's delta-debugging minimisation.

    Returns a subset of ``items`` that still satisfies ``still_fails`` and from
    which no single further element (at the granularity reached) can be removed.
    Standard algorithm, no cleverness: halve, test each chunk, then test each
    complement, then refine the granularity.
    """
    current = list(items)
    granularity = 2

    while len(current) >= 2:
        chunk_size = max(1, len(current) // granularity)
        chunks = [current[i : i + chunk_size] for i in range(0, len(current), chunk_size)]

        # Can one chunk alone reproduce it?
        reduced = False
        for chunk in chunks:
            if chunk and still_fails(chunk):
                current = chunk
                granularity = 2
                reduced = True
                break
        if reduced:
            continue

        # Can we drop one chunk?
        for chunk in chunks:
            complement = [item for item in current if item not in chunk]
            if complement and still_fails(complement):
                current = complement
                granularity = max(granularity - 1, 2)
                reduced = True
                break
        if reduced:
            continue

        if granularity >= len(current):
            break
        granularity = min(len(current), granularity * 2)

    return current


def shrink(
    runner: Runner,
    failing: Scenario,
    *,
    max_candidates: int = 4000,
    min_workload: int = 1,
) -> ShrinkReport:
    """Reduce a failing scenario to the smallest one that still reproduces it.

    Step 0 records what the original run actually did, because a scenario found by
    search carries only a seed -- the fault plan has to be recovered by running it
    once in sample mode.
    """
    baseline = runner(failing)
    if not baseline.violated or baseline.violation is None:
        raise ValueError(
            f"shrink() needs a scenario that violates an invariant; "
            f"{failing.describe()} came back as {baseline.status}"
        )

    target_invariant = baseline.violation.name
    original_plan = baseline.fault_plan
    original = replace(failing, plan=original_plan)

    tried = 1
    cache: dict[tuple, bool] = {}
    best = original
    best_result = baseline

    def attempt(candidate: Scenario) -> Optional[RunResult]:
        """Run a candidate; return its result only if it reproduces the same bug."""
        nonlocal tried
        key = (
            candidate.seed,
            candidate.workload,
            tuple(sorted((f.seq, f.kind, f.value) for f in (candidate.plan or FaultPlan()))),
        )
        if key in cache and not cache[key]:
            return None
        if tried >= max_candidates:
            return None
        tried += 1
        result = runner(candidate)
        reproduces = (
            result.violated
            and result.violation is not None
            and result.violation.name == target_invariant
        )
        cache[key] = reproduces
        return result if reproduces else None

    improved = True
    passes = 0
    while improved and passes < 6:
        improved = False
        passes += 1

        # 1. Fewer items. Done first: a smaller workload produces fewer messages,
        #    which shrinks the space the fault minimiser then has to search.
        if best.workload is not None and best.workload > min_workload:
            for size in range(min_workload, best.workload):
                candidate = replace(best, workload=size)
                result = attempt(candidate)
                if result is not None:
                    best, best_result, improved = candidate, result, True
                    break

        # 2. Fewer faults.
        if best.plan is not None and len(best.plan) > 0:
            def predicate(subset: Sequence[Fault]) -> bool:
                return attempt(replace(best, plan=FaultPlan.of(subset))) is not None

            minimal_faults = ddmin(list(best.plan), predicate)
            if len(minimal_faults) < len(best.plan):
                candidate = replace(best, plan=FaultPlan.of(minimal_faults))
                result = attempt(candidate)
                if result is not None:
                    best, best_result, improved = candidate, result, True

    return ShrinkReport(
        original=original,
        minimal=best,
        original_events=len(baseline.trace),
        minimal_events=len(best_result.trace),
        original_faults=len(original_plan),
        minimal_faults=len(best.plan) if best.plan is not None else 0,
        candidates_tried=tried,
        invariant=target_invariant,
        result=best_result,
    )
