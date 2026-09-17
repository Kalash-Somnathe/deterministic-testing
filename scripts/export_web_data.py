"""Export real `deterministic_testing` runs to JSON for the static web visualiser.

Nothing here invents a number. Every value written to ``web/data`` is either

* produced by running the real simulation through ``examples.pipeline``, or
* copied verbatim out of the committed result files in ``artifacts/``.

The browser does no simulation. It replays what this script recorded, which is
the same relationship the framework itself has with a seed: the run happens once,
the trace is the evidence, and everything afterwards reads the evidence.

Usage
-----
    python scripts/export_web_data.py            # skip work that is already fresh
    python scripts/export_web_data.py --force    # rebuild everything

Freshness: each output is compared against the mtimes of the framework sources,
the example under test, the artifacts and this script. An output that is newer
than all of them is left alone, so an interrupted run costs one file, not the set.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deterministic_testing.network import FaultConfig  # noqa: E402
from deterministic_testing.search import Scenario, ShrinkReport, shrink  # noqa: E402
from deterministic_testing.simulation import RunResult  # noqa: E402
from examples.pipeline import make_runner  # noqa: E402

DATA_DIR = ROOT / "web" / "data"
ARTIFACTS = ROOT / "artifacts"

# Lane order top to bottom. Chosen to match the direction work flows through the
# pipeline, so a message arrow almost always points downward on its first hop.
LANES = ["producer", "broker", "worker-a", "worker-b", "store"]

# How many seeds get a full stored trace, per variant, for the seed browser.
TRACE_SEEDS = 64
# How many seeds get a one-line outcome summary, per variant.
SUMMARY_SEEDS = 512

VARIANTS = ("buggy", "claim", "fixed")

# Events that carry no message and no useful payload of their own. Kept in the
# trace (they are part of what happened) but flagged so the UI can fade them.
QUIET_KINDS = {"SPAWN", "WAKE", "SLEEP", "EXIT"}
FAULT_KINDS = {"DROP", "DELAY", "DUPLICATE"}


# -- freshness -----------------------------------------------------------


def _newest_input_mtime() -> float:
    sources: list[Path] = [Path(__file__).resolve()]
    sources += sorted((ROOT / "deterministic_testing").glob("*.py"))
    sources += sorted((ROOT / "examples").glob("*.py"))
    sources += sorted(ARTIFACTS.glob("*.json"))
    return max((p.stat().st_mtime for p in sources if p.exists()), default=0.0)


def _is_fresh(path: Path, newest_input: float) -> bool:
    return path.exists() and path.stat().st_mtime > newest_input


def _write(path: Path, payload: Any) -> int:
    """Write compact JSON and report its size. Compact, because these are data
    files that ship to a browser, not files a person reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


# -- trace encoding ------------------------------------------------------


def _op_of(payload: Any) -> Optional[str]:
    return payload.get("op") if isinstance(payload, dict) else None


def encode_trace(
    result: RunResult,
    *,
    trace_id: str,
    variant: str,
    workload: int,
    title: str,
    subtitle: str,
    full_payloads: bool = True,
) -> dict:
    """Turn a RunResult into the shape the page draws.

    Messages are indexed by their sequence number so that a fault event, which
    records only ``seq``, can be drawn on the correct pair of lanes: the fault
    is attributed in the trace to the process that was executing at the time,
    which is not always the sender.
    """
    messages: dict[str, dict] = {}
    for event in result.trace:
        if event.kind != "SEND":
            continue
        seq = event.detail.get("seq")
        if seq is None:
            continue
        payload = event.detail.get("payload")
        entry = {
            "from": event.process,
            "to": event.detail.get("to"),
            "op": _op_of(payload),
        }
        if isinstance(payload, dict):
            if "key" in payload:
                entry["key"] = payload["key"]
            if full_payloads:
                entry["payload"] = payload
        messages[str(seq)] = entry

    events: list[dict] = []
    for event in result.trace:
        item: dict[str, Any] = {
            "step": event.step,
            "t": event.time_micros,
            "kind": event.kind,
            "proc": event.process,
        }
        seq = event.detail.get("seq")
        if seq is not None:
            item["seq"] = seq
        if event.kind in FAULT_KINDS:
            item["value"] = event.detail.get("value", 0)
        if event.kind == "LOG":
            item["log"] = {k: v for k, v in event.detail.items() if k != "seq"}
        if event.kind == "VIOLATION":
            item["violation"] = event.detail
        if event.kind in QUIET_KINDS:
            item["quiet"] = True
        events.append(item)

    lanes = [name for name in LANES if any(e["proc"] == name for e in events)]
    for name in sorted({e["proc"] for e in events} - set(lanes) - {"invariant"}):
        lanes.append(name)

    violation = None
    if result.violation is not None:
        violation = {
            "name": result.violation.name,
            "detail": result.violation.detail,
            "step": result.violation.step,
        }

    return {
        "id": trace_id,
        "title": title,
        "subtitle": subtitle,
        "seed": result.seed,
        "variant": variant,
        "workload": workload,
        "status": result.status,
        "steps": result.steps,
        "eventCount": len(result.trace),
        "faultCount": len(result.fault_plan),
        "endTime": result.end_time_micros,
        "digest": result.trace.digest(),
        "plan": result.fault_plan.to_json(),
        "violation": violation,
        "lanes": lanes,
        "messages": messages,
        "events": events,
    }


# -- shrink alignment ----------------------------------------------------


def _align_key(event: dict, messages: dict[str, dict]) -> tuple:
    """A canonical identity for an event, for diffing two runs against each other.

    Sequence numbers are useless for this: removing one fault renumbers every
    message after it. What survives is *what happened to whom* -- the kind, the
    process, and, for a message event, the operation and the link it travelled.
    """
    seq = event.get("seq")
    message = messages.get(str(seq)) if seq is not None else None
    if message is None:
        return (event["kind"], event["proc"], None, None, None)
    return (
        event["kind"],
        event["proc"],
        message.get("op"),
        message.get("from"),
        message.get("to"),
    )


def align(original: dict, shrunk: dict) -> list[Optional[int]]:
    """Longest common subsequence between two traces, on ``_align_key``.

    Returned list is parallel to ``shrunk["events"]``: each entry is the index of
    the matching event in the original trace, or ``None`` where the shrunk run
    did something the original did not. This is a presentation aid and nothing
    more -- the two runs are genuinely different executions, because removing a
    fault changes the message sequence. It is honest about that: unmatched events
    are reported, not hidden.
    """
    a = [_align_key(e, original["messages"]) for e in original["events"]]
    b = [_align_key(e, shrunk["messages"]) for e in shrunk["events"]]
    n, m = len(a), len(b)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, nxt = table[i], table[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if a[i] == b[j] else max(nxt[j], row[j + 1])

    mapping: list[Optional[int]] = [None] * m
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            mapping[j] = i
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return mapping


# -- runners -------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    variant: str
    items: int = 4
    faults: str = "realistic"

    def runner(self) -> Callable[[Scenario], RunResult]:
        config = FaultConfig.realistic() if self.faults == "realistic" else FaultConfig.none()
        return make_runner(config, variant=self.variant, default_items=self.items)


def build_featured(log: Callable[[str], None]) -> dict:
    """The two stories the page is built around, each with its shrunk twin."""
    featured: dict[str, dict] = {}
    shrink_stats: dict[str, dict] = {}

    stories = [
        {
            "key": "planted",
            "variant": "buggy",
            "seed": 1,
            "items": 4,
            "title": "The planted bug",
            "subtitle": "check the dedup table, credit, then mark -- with a network round trip in between",
            "artifact": "seed1_minimal.json",
        },
        {
            "key": "unplanted",
            "variant": "claim",
            "seed": 11,
            "items": 4,
            "title": "The bug in the fix",
            "subtitle": "atomic claim closes the window; replies correlated on (op, key) open another",
            "artifact": "seed11_claim_minimal.json",
        },
    ]

    for story in stories:
        case = Case(variant=story["variant"], items=story["items"])
        runner = case.runner()
        original_scenario = Scenario(seed=story["seed"], workload=story["items"])

        log(f"  running {story['variant']} seed {story['seed']} ...")
        original_result = runner(original_scenario)
        if not original_result.violated:
            raise SystemExit(
                f"expected {story['variant']} seed {story['seed']} to violate an invariant; "
                f"got {original_result.status}. The exported page would be wrong."
            )
        original = encode_trace(
            original_result,
            trace_id=f"{story['key']}-original",
            variant=story["variant"],
            workload=story["items"],
            title=story["title"],
            subtitle="the run as the search found it",
        )

        log(f"  shrinking {story['variant']} seed {story['seed']} ...")
        started = time.perf_counter()
        report: ShrinkReport = shrink(runner, original_scenario)
        elapsed = time.perf_counter() - started
        minimal_result = report.result
        shrunk = encode_trace(
            minimal_result,
            trace_id=f"{story['key']}-shrunk",
            variant=story["variant"],
            workload=report.minimal.workload or story["items"],
            title=story["title"],
            subtitle="the same failure, reduced",
        )

        featured[f"{story['key']}-original"] = original
        featured[f"{story['key']}-shrunk"] = shrunk
        shrink_stats[story["key"]] = {
            "variant": story["variant"],
            "seed": story["seed"],
            "title": story["title"],
            "subtitle": story["subtitle"],
            "originalEvents": report.original_events,
            "minimalEvents": report.minimal_events,
            "originalFaults": report.original_faults,
            "minimalFaults": report.minimal_faults,
            "originalWorkload": story["items"],
            "minimalWorkload": report.minimal.workload,
            "candidates": report.candidates_tried,
            "seconds": round(elapsed, 2),
            "invariant": report.invariant,
            "summary": report.summary(),
            "minimalScenario": report.minimal.to_json(),
            # Verbatim, in the key order `deterministic-testing shrink --save` writes it, so the page
            # shows the artifact rather than a reformatted copy of it.
            "minimalText": json.dumps(report.minimal.to_json(), indent=2),
            # Size of the scenario file the CLI actually wrote into artifacts/, so the
            # page and the README quote the same number.
            "minimalBytes": (ARTIFACTS / story["artifact"]).stat().st_size,
            "artifact": "artifacts/" + story["artifact"],
            "align": align(original, shrunk),
        }
        log(f"    {report.summary()}")

        stored = json.loads((ARTIFACTS / story["artifact"]).read_text(encoding="utf-8"))
        if stored != report.minimal.to_json():
            raise SystemExit(
                f"the shrunk scenario for {story['key']} does not match "
                f"artifacts/{story['artifact']}; the page would contradict the repository."
            )

    return {"traces": featured, "shrink": shrink_stats}


def build_seedbank(log: Callable[[str], None]) -> dict:
    """A stored bank of real runs, so the seed input answers with evidence."""
    bank: dict[str, dict] = {}
    index: dict[str, dict] = {}

    for variant in VARIANTS:
        runner = Case(variant=variant).runner()
        traces: dict[str, dict] = {}
        summaries: dict[str, list] = {}
        log(f"  {variant}: {SUMMARY_SEEDS} seeds ({TRACE_SEEDS} stored in full) ...")
        for seed in range(SUMMARY_SEEDS):
            result = runner(Scenario(seed=seed, workload=4))
            # [status, events, steps, faults, endTime, digest12]
            summaries[str(seed)] = [
                result.status,
                len(result.trace),
                result.steps,
                len(result.fault_plan),
                result.end_time_micros,
                result.trace.digest()[:12],
            ]
            if seed < TRACE_SEEDS:
                traces[str(seed)] = encode_trace(
                    result,
                    trace_id=f"{variant}-{seed}",
                    variant=variant,
                    workload=4,
                    title=f"seed {seed}",
                    subtitle=variant,
                    full_payloads=False,
                )
        violations = sum(1 for v in summaries.values() if v[0] == "violation")
        bank[variant] = {"traces": traces}
        index[variant] = {
            "summaries": summaries,
            "violations": violations,
            "seedsSampled": SUMMARY_SEEDS,
            "tracesStored": TRACE_SEEDS,
        }
        log(f"    {violations}/{SUMMARY_SEEDS} violated")

    return {"bank": bank, "index": index}


def build_determinism(log: Callable[[str], None], runs: int = 100) -> dict:
    """Run one seed many times and count the distinct digests. Must be 1."""
    runner = Case(variant="buggy").runner()
    log(f"  replaying seed 1 x{runs} ...")
    started = time.perf_counter()
    digests = {runner(Scenario(seed=1, workload=4)).trace.digest() for _ in range(runs)}
    elapsed = time.perf_counter() - started
    log(f"    {len(digests)} distinct digest(s) in {elapsed:.2f}s")
    return {
        "runs": runs,
        "distinctDigests": len(digests),
        "digest": sorted(digests)[0],
        "seconds": round(elapsed, 3),
    }


def load_artifacts() -> dict:
    """Committed measurements, copied verbatim. Not recomputed here: the matrix
    is 60,000 runs and takes minutes, and the repository already has its output.
    """
    matrix = json.loads((ARTIFACTS / "variant_matrix.json").read_text(encoding="utf-8"))
    throughput = json.loads((ARTIFACTS / "throughput.json").read_text(encoding="utf-8"))
    return {"matrix": matrix, "throughput": throughput}


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="rebuild even if outputs are fresh")
    parser.add_argument("--out", type=Path, default=DATA_DIR, help="output directory")
    args = parser.parse_args(list(argv) if argv is not None else None)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    newest = _newest_input_mtime()

    def log(message: str) -> None:
        print(message, flush=True)

    written: list[tuple[str, int]] = []

    featured_path = out / "featured.json"
    seedbank_paths = {v: out / f"seeds-{v}.json" for v in VARIANTS}
    manifest_path = out / "manifest.json"

    featured: Optional[dict] = None
    if args.force or not _is_fresh(featured_path, newest):
        log("featured traces")
        featured = build_featured(log)
        written.append((featured_path.name, _write(featured_path, featured)))
    else:
        log(f"featured traces: {featured_path.name} is fresh, skipping")
        featured = json.loads(featured_path.read_text(encoding="utf-8"))

    need_bank = args.force or any(not _is_fresh(p, newest) for p in seedbank_paths.values())
    seed_index: Optional[dict] = None
    if need_bank:
        log("seed bank")
        built = build_seedbank(log)
        for variant, path in seedbank_paths.items():
            written.append((path.name, _write(path, built["bank"][variant])))
        seed_index = built["index"]
    else:
        log("seed bank: fresh, skipping")

    if args.force or not _is_fresh(manifest_path, newest) or seed_index is not None:
        if seed_index is None:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            seed_index = existing["seeds"]
            determinism = existing["determinism"]
        else:
            log("determinism proof")
            determinism = build_determinism(log)
        manifest = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "python": sys.version.split()[0],
            "seeds": seed_index,
            "determinism": determinism,
            "shrink": featured["shrink"],
            **load_artifacts(),
        }
        written.append((manifest_path.name, _write(manifest_path, manifest)))
    else:
        log(f"manifest: {manifest_path.name} is fresh, skipping")

    total = sum(p.stat().st_size for p in out.glob("*.json"))
    for name, size in written:
        log(f"  wrote {name:<22} {size / 1024:>8.1f} KB")
    log(f"total in {out}: {total / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
