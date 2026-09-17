"""Command line interface.

    deterministic-testing search --seeds 1000   hunt for a failing seed
    deterministic-testing replay 1              reproduce one seed, print the trace
    deterministic-testing shrink 1              reduce a failing seed to a minimal scenario
    deterministic-testing verify --runs 100     prove the same seed gives the same trace
    deterministic-testing demo                  the whole worked example, end to end

The CLI deliberately knows nothing about the pipeline beyond importing it. The
framework is a library; the system under test is somebody else's code. Keeping
that boundary visible matters, because the interesting question about a tool like
this is always "what would it take to point it at my system", and the answer here
is: write generators, declare invariants, hand it a runner.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

from .network import FaultConfig
from .search import Scenario, search, shrink
from .simulation import RunResult, RunStatus
from .trace import Trace

INTERESTING_KINDS = ("SEND", "DROP", "DELAY", "DUPLICATE", "TIMEOUT", "LOG", "VIOLATION", "EXIT")
"""The event kinds that carry the story. DELIVER/RECV pairs are mechanical noise
once you know a message was sent and to whom, so ``--focus`` hides them."""


def _load_pipeline() -> ModuleType:
    """Import the example system under test, working from a source checkout too."""
    try:
        from examples import pipeline  # type: ignore[import-not-found]
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from examples import pipeline  # type: ignore[import-not-found]
    return pipeline


FAULT_PROFILES: dict[str, FaultConfig] = {
    "none": FaultConfig.none(),
    "realistic": FaultConfig.realistic(),
    "mild": FaultConfig(
        base_latency=0.001,
        drop_probability=0.005,
        duplicate_probability=0.005,
        delay_probability=0.02,
        delay_min=0.4,
        delay_max=0.9,
        max_duplicates=2,
    ),
}


def _print_trace(trace: Trace, *, focus: bool, limit: Optional[int]) -> None:
    shown = trace.filtered(INTERESTING_KINDS) if focus else trace
    text = shown.pretty(limit=limit)
    if text:
        print(text)
    print(f"  ({len(shown)} of {len(trace)} events shown)")


def _describe(result: RunResult) -> str:
    if result.violation is not None:
        return f"VIOLATION: {result.violation}"
    if result.error is not None:
        return f"{result.status.upper()}: {result.error}"
    return "completed cleanly"


# -- commands ------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    pipeline = _load_pipeline()
    runner = pipeline.make_runner(
        FAULT_PROFILES[args.faults], variant=args.variant, default_items=args.items
    )
    checkpoint = Path(args.checkpoint) if args.checkpoint else None

    print(
        f"searching seeds {args.start}..{args.start + args.seeds - 1} "
        f"[faults={args.faults} items={args.items} "
        f"worker={args.variant}]"
    )
    report = search(
        runner,
        range(args.start, args.start + args.seeds),
        workload=args.items,
        stop_on_first=not args.all,
        checkpoint=checkpoint,
    )
    print(report.summary())

    if not report.failures:
        print("no violation found; the invariants held for every seed tried")
        return 0

    seed, status, detail = report.failures[0]
    print(f"\nfirst failing seed: {seed}  ({status})")
    print(f"  {detail}")
    print(f"\nreproduce with:  deterministic-testing replay {seed} --items {args.items} --faults {args.faults}")
    print(f"minimise with:   deterministic-testing shrink {seed} --items {args.items} --faults {args.faults}")
    return 1


def cmd_replay(args: argparse.Namespace) -> int:
    pipeline = _load_pipeline()
    runner = pipeline.make_runner(
        FAULT_PROFILES[args.faults], variant=args.variant, default_items=args.items
    )
    scenario = Scenario(seed=args.seed, workload=args.items)
    if args.plan:
        scenario = Scenario.from_json(json.loads(Path(args.plan).read_text(encoding="utf-8")))

    result = runner(scenario)
    print(f"replay {scenario.describe()}")
    print(f"  {result.summary()}")
    print(f"  {_describe(result)}")
    print()
    _print_trace(result.trace, focus=args.focus, limit=args.limit)
    print(f"\ntrace digest: {result.digest()}")
    return 1 if result.failed else 0


def cmd_shrink(args: argparse.Namespace) -> int:
    pipeline = _load_pipeline()
    runner = pipeline.make_runner(
        FAULT_PROFILES[args.faults], variant=args.variant, default_items=args.items
    )
    report = shrink(runner, Scenario(seed=args.seed, workload=args.items))

    print(report.summary())
    print(f"\nminimal scenario: {report.minimal.describe()}")
    print(f"invariant violated: {report.invariant}")
    print("faults required:")
    print(report.minimal.plan.describe() if report.minimal.plan else "  (none)")
    print()
    _print_trace(report.result.trace, focus=args.focus, limit=args.limit)

    if args.save:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.minimal.to_json(), indent=2), encoding="utf-8")
        print(f"\nminimal scenario written to {path}")
        print(f"replay it with:  deterministic-testing replay {report.minimal.seed} --plan {path}")
    return 1


def cmd_verify(args: argparse.Namespace) -> int:
    """The determinism proof, runnable by hand.

    The same claim is asserted in the test suite; having it as a command matters
    because "run it yourself on your machine" is a more convincing answer to
    "how do you know it is deterministic" than any amount of prose.
    """
    pipeline = _load_pipeline()
    runner = pipeline.make_runner(
        FAULT_PROFILES[args.faults], variant=args.variant, default_items=args.items
    )
    scenario = Scenario(seed=args.seed, workload=args.items)

    digests: set[str] = set()
    for _ in range(args.runs):
        digests.add(runner(scenario).digest())

    if len(digests) == 1:
        print(f"seed {args.seed}: {args.runs} runs, 1 distinct trace digest")
        print(f"  {digests.pop()}")
        print("determinism holds")
        return 0
    print(f"seed {args.seed}: {args.runs} runs produced {len(digests)} DIFFERENT digests")
    for digest in sorted(digests):
        print(f"  {digest}")
    print("DETERMINISM IS BROKEN -- every seed this tool has reported is void")
    return 2


def cmd_demo(args: argparse.Namespace) -> int:
    """The whole story in one command, for someone who has thirty seconds."""
    pipeline = _load_pipeline()
    items = args.items

    print("=" * 72)
    print("1. The conventional test: a perfect network, run many times.")
    print("=" * 72)
    clean = pipeline.make_runner(FaultConfig.none(), default_items=items)
    failures = sum(1 for seed in range(args.baseline) if clean(Scenario(seed, items)).failed)
    print(f"   {args.baseline} runs on a perfect network: {failures} failures")
    print("   The suite is green. Ship it.\n")

    print("=" * 72)
    print("2. deterministic-testing: the same code, with a network that drops and delays.")
    print("=" * 72)
    buggy = pipeline.make_runner(FaultConfig.realistic(), variant="buggy", default_items=items)
    report = search(buggy, range(0, args.seeds), workload=items, stop_on_first=True)
    print(f"   {report.summary()}")
    if not report.failures:
        print("   no violation found in this range")
        return 0
    seed = report.failures[0][0]
    print(f"   first failing seed: {seed}")
    print(f"   {report.failures[0][2]}\n")

    print("=" * 72)
    print("3. Shrink it to something a human can read.")
    print("=" * 72)
    shrunk = shrink(buggy, Scenario(seed=seed, workload=items))
    print(f"   {shrunk.summary()}")
    print(f"   minimal: {shrunk.minimal.describe()}")
    print(shrunk.minimal.plan.describe() if shrunk.minimal.plan else "   (no faults)")
    print()
    _print_trace(shrunk.result.trace, focus=True, limit=None)

    print()
    print("=" * 72)
    print("4. First fix: make the check and the mark one atomic claim.")
    print("=" * 72)
    claim = pipeline.make_runner(FaultConfig.realistic(), variant="claim", default_items=items)
    claim_report = search(claim, range(0, args.seeds), workload=items, stop_on_first=False)
    print(f"   {claim_report.summary()}")
    if claim_report.failures:
        first = claim_report.failures[0]
        print(f"   Still failing. First failing seed: {first[0]}")
        print(f"   {first[2]}")
        print()
        print("   That fix was real -- it closed the window the planted bug used --")
        print("   and it is still wrong. The remaining bug was not planted: replies")
        print("   are correlated by (op, key), so a duplicated reply to an earlier")
        print("   request is indistinguishable from the answer to this one.")
        print("   In production this would look like a fix: the failure rate drops")
        print("   several-fold, from 'most days' to 'about weekly'.")
    else:
        print(f"   {args.seeds} seeds, no violation in this range.")

    print()
    print("=" * 72)
    print("5. Real fix: one idempotent apply, replies correlated by request id.")
    print("=" * 72)
    fixed = pipeline.make_runner(FaultConfig.realistic(), variant="fixed", default_items=items)
    fixed_report = search(fixed, range(0, args.seeds), workload=items, stop_on_first=False)
    print(f"   {fixed_report.summary()}")
    if fixed_report.failures:
        print("   still failing:")
        for entry in fixed_report.failures[:5]:
            print(f"     seed {entry[0]}: {entry[1]} {entry[2]}")
        return 1
    print(f"   {args.seeds} seeds, no violation.")
    return 0


# -- wiring --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deterministic-testing",
        description="Deterministic simulation testing: turn a once-a-week production "
        "bug into a seed number.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--items", type=int, default=4, help="workload size (items produced)")
        sub.add_argument(
            "--faults",
            choices=sorted(FAULT_PROFILES),
            default="realistic",
            help="network fault profile",
        )
        sub.add_argument(
            "--variant",
            choices=["buggy", "claim", "fixed"],
            default="buggy",
            help="which consumer to run: the planted bug, the first (insufficient) "
            "fix, or the corrected one",
        )

    search_parser = subparsers.add_parser("search", help="hunt many seeds for a violation")
    common(search_parser)
    search_parser.add_argument("--seeds", type=int, default=1000, help="how many seeds to try")
    search_parser.add_argument("--start", type=int, default=0, help="first seed")
    search_parser.add_argument("--all", action="store_true", help="do not stop at the first failure")
    search_parser.add_argument("--checkpoint", help="JSONL file; makes the search resumable")
    search_parser.set_defaults(func=cmd_search)

    replay_parser = subparsers.add_parser("replay", help="reproduce one seed exactly")
    common(replay_parser)
    replay_parser.add_argument("seed", type=int)
    replay_parser.add_argument("--limit", type=int, default=None, help="show only the last N events")
    replay_parser.add_argument("--focus", action="store_true", help="hide DELIVER/RECV bookkeeping")
    replay_parser.add_argument("--plan", help="replay a saved minimal scenario JSON file")
    replay_parser.set_defaults(func=cmd_replay)

    shrink_parser = subparsers.add_parser("shrink", help="reduce a failing seed to a minimal scenario")
    common(shrink_parser)
    shrink_parser.add_argument("seed", type=int)
    shrink_parser.add_argument("--limit", type=int, default=None)
    shrink_parser.add_argument("--focus", action="store_true", help="hide DELIVER/RECV bookkeeping")
    shrink_parser.add_argument("--save", help="write the minimal scenario to this JSON file")
    shrink_parser.set_defaults(func=cmd_shrink)

    verify_parser = subparsers.add_parser("verify", help="prove one seed replays identically")
    common(verify_parser)
    verify_parser.add_argument("--seed", type=int, default=1)
    verify_parser.add_argument("--runs", type=int, default=100)
    verify_parser.set_defaults(func=cmd_verify)

    demo_parser = subparsers.add_parser("demo", help="the whole worked example, end to end")
    demo_parser.add_argument("--items", type=int, default=4)
    demo_parser.add_argument("--seeds", type=int, default=200)
    demo_parser.add_argument("--baseline", type=int, default=1000)
    demo_parser.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
