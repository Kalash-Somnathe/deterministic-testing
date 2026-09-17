"""The system under test: a small distributed data pipeline.

    producer  ->  broker (at-least-once, retry on timeout)  ->  worker-a / worker-b
                                                                     |
                                                                     v
                                                                   store

This is written the way a competent engineer writes it on a Tuesday. It is not a
puzzle and the bug is not decorative -- it is the single most common idempotency
mistake in message-driven systems, and it is invisible to any test that does not
control the interleaving.

**Where the bug is.** ``worker_buggy`` performs the classic three-step dance:

    1. ask the store "have I already processed this key?"     (check)
    2. if not, apply the side effect                          (credit)
    3. record that the key is done                            (mark)

Steps 1 and 3 are separated by a network round trip. Between them, the same key
can be checked by the *other* worker, which also sees "not seen", and also
credits. One item, two credits. Nothing in the code looks wrong; each worker is
individually correct; the ordering that breaks it needs a delayed ack, a retry,
and the retry landing on the other worker.

**The fix.** ``worker_fixed`` collapses check-and-mark into a single ``claim``
message. The store is one process handling one message at a time, so ``claim`` is
atomic by construction -- exactly as ``INSERT ... ON CONFLICT DO NOTHING`` is
atomic in a real database. The loser of the claim acks and does nothing.

The fix is not free, and it is worth being honest about the trade: claiming before
the side effect converts at-least-once into at-most-once. If the credit fails
after a successful claim, the item is silently dropped. Production systems resolve
this with a two-phase record (claim as *in progress*, then mark *committed*, with
a sweeper for the in-between state) or by making the side effect itself
conditional on the key. The narrow point being demonstrated here is that
`deterministic_testing` distinguishes the two orderings -- and a conventional test suite does not.

Everything a real service would get from the environment -- time, randomness,
message delivery -- is yielded to the scheduler instead. The runtime guard in
``deterministic_testing.guards`` makes that mandatory rather than merely encouraged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from deterministic_testing import (
    FaultConfig,
    Log,
    Message,
    ProcessGen,
    Random,
    Recv,
    RunResult,
    Send,
    Simulation,
    Sleep,
)
from deterministic_testing.search import Scenario

__all__ = [
    "StoreState",
    "PipelineConfig",
    "build_pipeline",
    "make_runner",
    "expected_keys",
    "worker_names",
    "MAX_ATTEMPTS",
    "RETRY_TIMEOUT",
    "STORE_TIMEOUT",
    "IDLE_TIMEOUT",
]

# Virtual-time constants. Every one of these is free: the whole point of the
# virtual clock is that a 30-second idle timeout costs no wall-clock time at all.
RETRY_TIMEOUT = 0.5
"""Seconds the broker waits for an ack before redelivering."""

STORE_TIMEOUT = 0.25
"""Seconds a worker waits for a store reply before abandoning the attempt."""

IDLE_TIMEOUT = 30.0
"""Seconds of silence after which a long-lived process shuts itself down.

This is how the simulation terminates cleanly without shutdown messages, which
could themselves be dropped by the fault injector and produce spurious deadlocks.
It costs nothing, because no one actually waits thirty seconds.
"""

MAX_ATTEMPTS = 8
"""Broker redelivery budget per item."""


@dataclass(slots=True)
class StoreState:
    """The durable side of the pipeline. Owned by exactly one process.

    ``credits`` is the evidence: an ordered list of every credit that was applied.
    If a key appears in it twice, the pipeline double-counted, and the list says
    which key and in what order. That is the difference between "the totals are
    off by one" and a bug report.
    """

    total: int = 0
    credits: list[str] = field(default_factory=list)
    processed: set[str] = field(default_factory=set)
    """Dedup table. A set is what you would really write here.

    Membership tests on a set are deterministic; *iteration* order is not, because
    str hashes are salted per process. Nothing in this file ever iterates it, and
    ``test_determinism.py`` runs the whole pipeline under several different
    PYTHONHASHSEED values to keep that honest.
    """

    def duplicate_credits(self) -> list[str]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for key in self.credits:  # list, so this iteration IS ordered
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return duplicates


@dataclass(slots=True)
class PipelineConfig:
    """Workload shape. ``item_count`` is one of the dimensions shrinking reduces."""

    item_count: int = 4
    worker_count: int = 2
    variant: str = "buggy"
    """Which consumer to run. The three are a narrative, in order:

    ``buggy``  check -> credit -> mark. The planted bug.
    ``claim``  atomic claim -> credit. Fixes the planted bug; `deterministic_testing` then found
               a second, unplanted one (stale duplicated reply matched by a retry).
    ``fixed``  one idempotent apply, correlated by request id. Fixes both.
    """
    produce_interval: float = 0.05
    """Seconds between item submissions."""


def expected_keys(config: PipelineConfig) -> list[str]:
    return [f"item-{index}" for index in range(config.item_count)]


def worker_names(config: PipelineConfig) -> list[str]:
    """Sorted list, never a set -- this is indexed by an RNG draw."""
    return sorted(f"worker-{chr(ord('a') + index)}" for index in range(config.worker_count))


# -- predicates for selective receive ------------------------------------
# Pure functions of the message. They run inside the scheduler, so a side effect
# in here would be non-determinism the guard cannot detect.


def _is_op(*ops: str) -> Callable[[Message], bool]:
    wanted = frozenset(ops)

    def predicate(message: Message) -> bool:
        return message.payload.get("op") in wanted

    return predicate


def _is_reply(op: str, key: str) -> Callable[[Message], bool]:
    """Correlate a reply by operation and key only.

    This looks obviously fine and is not. It cannot tell a reply to *this* request
    from a duplicated reply to a *previous* request for the same key. `deterministic_testing`
    found that; see ``worker_claim`` below.
    """

    def predicate(message: Message) -> bool:
        return message.payload.get("op") == op and message.payload.get("key") == key

    return predicate


def _is_reply_to(request_id: str) -> Callable[[Message], bool]:
    """Correlate a reply by a unique per-request id. The correct version.

    ``request_id`` is built from a per-worker counter, not ``uuid4()``: uuid4 draws
    from ``os.urandom`` and would be blocked by the determinism guard, and quite
    rightly, since it would make the run unreplayable.
    """

    def predicate(message: Message) -> bool:
        return message.payload.get("req") == request_id

    return predicate


# -- processes -----------------------------------------------------------


def producer(config: PipelineConfig) -> ProcessGen:
    """Submits ``item_count`` items to the broker, then exits."""
    for key in expected_keys(config):
        yield Send("broker", {"op": "submit", "key": key, "amount": 1})
        yield Sleep(config.produce_interval)
    yield Log("produced", {"count": config.item_count})


def broker(config: PipelineConfig) -> ProcessGen:
    """A queue with at-least-once delivery: retry until acked, or give up.

    At-least-once is the correct choice here and is *not* the bug. It is the
    contract that makes the consumer's idempotency the load-bearing property --
    which is exactly the point.
    """
    workers = worker_names(config)
    while True:
        job = yield Recv(timeout=IDLE_TIMEOUT, match=_is_op("submit"))
        if job is None:
            yield Log("broker_idle_shutdown")
            return

        key = job.payload["key"]
        amount = job.payload["amount"]
        acked = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Load balancing. Random rather than round-robin on purpose: it lets
            # the search explore both "retry hits the same worker" (safe, the
            # worker is single-threaded) and "retry hits the other worker"
            # (the dangerous case), instead of hard-coding the dangerous one.
            draw = yield Random()
            target = workers[int(draw * len(workers)) % len(workers)]

            yield Send(
                target,
                {
                    "op": "job",
                    "key": key,
                    "amount": amount,
                    "attempt": attempt,
                    "reply_to": "broker",
                },
            )
            ack = yield Recv(timeout=RETRY_TIMEOUT, match=_is_reply("ack", key))
            if ack is not None:
                acked = True
                break

        if not acked:
            yield Log("gave_up", {"key": key})


def store(state: StoreState) -> ProcessGen:
    """The database. One process, one message at a time.

    Single-threadedness is what makes ``claim`` atomic. That is not a simulation
    convenience -- it is the same guarantee a real database gives you for a single
    conditional statement, and the same guarantee you lose the moment you split it
    into a SELECT and a later INSERT.

    State is passed in rather than held in a module global, so that two
    simulations constructed in the same interpreter cannot possibly share it. A
    leaked global would make run N depend on run N-1, which is precisely the class
    of hidden coupling this whole framework exists to eliminate.
    """
    while True:
        message = yield Recv(timeout=IDLE_TIMEOUT)
        if message is None:
            yield Log("store_idle_shutdown", {"total": state.total})
            return

        payload = message.payload
        operation = payload["op"]
        key = payload.get("key")
        reply_to = payload.get("reply_to")

        request_id = payload.get("req")

        if operation == "check":
            yield Send(
                reply_to,
                {"op": "check_reply", "key": key, "req": request_id, "seen": key in state.processed},
            )
        elif operation == "credit":
            state.total += payload["amount"]
            state.credits.append(key)
            yield Log("credit", {"key": key, "total": state.total})
            yield Send(reply_to, {"op": "credit_reply", "key": key, "req": request_id})
        elif operation == "mark":
            state.processed.add(key)
            yield Send(reply_to, {"op": "mark_reply", "key": key, "req": request_id})
        elif operation == "claim":
            won = key not in state.processed
            if won:
                state.processed.add(key)
            yield Send(reply_to, {"op": "claim_reply", "key": key, "req": request_id, "won": won})
        elif operation == "apply":
            # The dedup test and the side effect in a single handler. Because the
            # store is one process handling one message at a time, this is atomic
            # in exactly the sense that a single conditional SQL statement is
            # atomic. There is no window, so there is nothing to race, and a
            # network-level duplicate of this very message is a no-op.
            applied = key not in state.processed
            if applied:
                state.processed.add(key)
                state.total += payload["amount"]
                state.credits.append(key)
                yield Log("credit", {"key": key, "total": state.total})
            yield Send(
                reply_to,
                {"op": "apply_reply", "key": key, "req": request_id, "applied": applied},
            )
        else:  # pragma: no cover - defensive
            raise ValueError(f"store received unknown op {operation!r}")


def worker_buggy(name: str) -> ProcessGen:
    """check -> credit -> mark.

    Every line is defensible in isolation. The window between the check in step 1
    and the mark in step 3 spans two network round trips, and during it the dedup
    table still says "not seen" -- so a redelivery of the same key to the other
    worker credits it a second time.
    """
    while True:
        job = yield Recv(timeout=IDLE_TIMEOUT, match=_is_op("job"))
        if job is None:
            yield Log("worker_idle_shutdown")
            return

        key = job.payload["key"]
        reply_to = job.payload["reply_to"]

        yield Send("store", {"op": "check", "key": key, "reply_to": name})
        checked = yield Recv(timeout=STORE_TIMEOUT, match=_is_reply("check_reply", key))
        if checked is None:
            continue  # store unreachable; do not ack, let the broker retry
        if checked.payload["seen"]:
            yield Send(reply_to, {"op": "ack", "key": key})
            continue

        yield Send(
            "store",
            {"op": "credit", "key": key, "amount": job.payload["amount"], "reply_to": name},
        )
        credited = yield Recv(timeout=STORE_TIMEOUT, match=_is_reply("credit_reply", key))
        if credited is None:
            continue

        # The bug lives on the next two lines: the key is only recorded now, long
        # after the side effect it was supposed to guard.
        yield Send("store", {"op": "mark", "key": key, "reply_to": name})
        marked = yield Recv(timeout=STORE_TIMEOUT, match=_is_reply("mark_reply", key))
        if marked is None:
            continue

        yield Send(reply_to, {"op": "ack", "key": key})


def worker_claim(name: str) -> ProcessGen:
    """claim -> credit. The first attempt at a fix, and it is still wrong.

    One atomic test-and-set replaces the check/mark pair, so the window that
    ``worker_buggy`` leaves open is gone. This *does* fix the planted bug.

    `deterministic_testing` then found a second one, which was not planted and which I did not
    see coming. Replies here are correlated by ``(op, key)``, which cannot
    distinguish a reply to *this* request from a duplicated reply to a *previous*
    request for the same key. Concretely, from the shrunk trace of seed 11:

        1. worker-a claims item-3 and wins.
        2. The network duplicates the ``claim_reply(won=True)``. One copy is
           consumed; the other sits in worker-a's mailbox.
        3. worker-a credits, acks, and the ack is delayed past the retry timeout.
        4. The broker redelivers item-3 to worker-a.
        5. worker-a sends a fresh claim -- and its selective receive immediately
           matches the *stale duplicate* from step 2, which says ``won=True``.
        6. worker-a credits item-3 a second time.

    The atomic claim is correct. The reply correlation is not. This is the classic
    missing-request-id bug, and it is the sort of thing that produces one bad row a
    week and no explanation.
    """
    while True:
        job = yield Recv(timeout=IDLE_TIMEOUT, match=_is_op("job"))
        if job is None:
            yield Log("worker_idle_shutdown")
            return

        key = job.payload["key"]
        reply_to = job.payload["reply_to"]

        yield Send("store", {"op": "claim", "key": key, "reply_to": name})
        claimed = yield Recv(timeout=STORE_TIMEOUT, match=_is_reply("claim_reply", key))
        if claimed is None:
            continue
        if not claimed.payload["won"]:
            yield Send(reply_to, {"op": "ack", "key": key})
            continue

        yield Send(
            "store",
            {"op": "credit", "key": key, "amount": job.payload["amount"], "reply_to": name},
        )
        credited = yield Recv(timeout=STORE_TIMEOUT, match=_is_reply("credit_reply", key))
        if credited is None:
            continue

        yield Send(reply_to, {"op": "ack", "key": key})


def worker_fixed(name: str) -> ProcessGen:
    """A single idempotent ``apply``, correlated by a unique request id.

    Two changes, each closing one of the two bugs `deterministic_testing` found:

    * **Atomicity.** The dedup test and the side effect are one message, handled
      in one store step. There is no window between them for anything to
      interleave into, and a network-level duplicate of the request itself is a
      no-op rather than a second credit. This is the same reasoning that makes
      ``INSERT ... ON CONFLICT DO NOTHING`` safe where ``SELECT`` then ``INSERT``
      is not.
    * **Correlation.** Every request carries a unique id and replies are matched
      on it, so a duplicated reply to an earlier request can never be mistaken for
      the answer to this one. The id comes from a per-worker counter -- not
      ``uuid4()``, which the determinism guard blocks precisely because it would
      make the run unreplayable.

    The honest cost of claiming before crediting has not gone away, it has been
    absorbed: because the claim and the credit are the same operation, there is no
    state in which a key is claimed but uncredited, and so no at-most-once hole to
    sweep up afterwards. That is the whole reason to prefer one atomic write over
    two correct ones.
    """
    counter = 0
    while True:
        job = yield Recv(timeout=IDLE_TIMEOUT, match=_is_op("job"))
        if job is None:
            yield Log("worker_idle_shutdown")
            return

        key = job.payload["key"]
        reply_to = job.payload["reply_to"]
        counter += 1
        request_id = f"{name}-{counter}"

        yield Send(
            "store",
            {
                "op": "apply",
                "key": key,
                "amount": job.payload["amount"],
                "req": request_id,
                "reply_to": name,
            },
        )
        applied = yield Recv(timeout=STORE_TIMEOUT, match=_is_reply_to(request_id))
        if applied is None:
            continue  # store unreachable; do not ack, let the broker retry

        yield Send(reply_to, {"op": "ack", "key": key})


# -- wiring --------------------------------------------------------------


def build_pipeline(simulation: Simulation, config: PipelineConfig) -> StoreState:
    """Spawn every process and declare the invariants. Returns the store state.

    The invariant is deliberately a *safety* property -- "never credit the same
    key twice" -- rather than an assertion about the final total. Safety
    properties can be checked after every single scheduler step, which is what
    turns "the numbers came out wrong" into "step 231, immediately after this
    delivery".
    """
    state = StoreState()

    simulation.spawn("producer", producer(config))
    simulation.spawn("broker", broker(config))
    simulation.spawn("store", store(state))

    try:
        worker_factory = WORKER_VARIANTS[config.variant]
    except KeyError:
        raise ValueError(
            f"unknown worker variant {config.variant!r}; "
            f"expected one of {sorted(WORKER_VARIANTS)}"
        ) from None
    for name in worker_names(config):
        simulation.spawn(name, worker_factory(name))

    simulation.invariant(
        "each item credited at most once",
        lambda: len(state.credits) == len(set(state.credits)),
        describe=lambda: (
            f"duplicate credits for {state.duplicate_credits()}; "
            f"credits={state.credits} total={state.total}"
        ),
    )
    simulation.invariant(
        "total never exceeds the number of submitted items",
        lambda: state.total <= config.item_count,
        describe=lambda: f"total={state.total} > item_count={config.item_count}",
    )
    return state


WORKER_VARIANTS: dict[str, Callable[[str], ProcessGen]] = {
    "buggy": worker_buggy,
    "claim": worker_claim,
    "fixed": worker_fixed,
}


def make_runner(
    fault_config: FaultConfig,
    *,
    variant: str = "buggy",
    default_items: int = 4,
    max_steps: int = 200_000,
) -> Callable[[Scenario], RunResult]:
    """Build the pure ``Scenario -> RunResult`` function the search driver needs.

    Purity is not decoration here. The shrinker runs thousands of candidates and
    compares their outcomes; if any state leaked between calls, the comparisons
    would be meaningless and the "minimal" scenario would be fiction. Every call
    below constructs a fresh ``Simulation``, fresh generators and a fresh
    ``StoreState``.
    """

    def run(scenario: Scenario) -> RunResult:
        config = PipelineConfig(
            item_count=scenario.workload if scenario.workload is not None else default_items,
            variant=variant,
        )
        simulation = Simulation(
            scenario.seed,
            fault_config=fault_config,
            fault_plan=scenario.plan,
            max_steps=max_steps,
        )
        build_pipeline(simulation, config)
        return simulation.run()

    return run
