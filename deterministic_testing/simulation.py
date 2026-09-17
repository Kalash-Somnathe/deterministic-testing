"""The deterministic scheduler.

This is the centre of the project. Everything else is scaffolding around one loop:

    while something is enabled:
        pick one enabled transition, using the seeded RNG
        perform it
        check the invariants

"Enabled transition" means either *a process that can take its next step* or *a
message that has reached its delivery time*. Both live in one list, and the choice
between them is a single draw from the scheduling sub-stream. That is why message
reordering happens without a dedicated reorder fault: two deliverable messages are
two enabled transitions, and which one goes first is the seed's decision.

Time is not in that loop. The clock moves only when nothing is enabled, and then
it jumps directly to the next instant at which something becomes enabled. A test
of a thirty-second timeout therefore costs microseconds.

Determinism rules obeyed throughout this file, each of which was a real hazard:

* Processes live in a ``dict`` keyed by name and are always iterated in insertion
  order. Never a ``set``: set iteration order depends on hash values, which for
  ``str`` are salted per process by ``PYTHONHASHSEED``.
* The enabled-transitions list is built in a fixed order (processes by spawn index,
  then messages by monotonic sequence) before the RNG is consulted. The RNG picks
  an index into a list whose construction never varied.
* Every identifier is a counter. No ``id()``, no ``uuid4()``, no ``hash()``.
* Ties are broken by integer index, never by object comparison.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .clock import VirtualClock, to_micros, to_seconds
from .errors import Deadlock, InvariantViolation, StepLimitExceeded
from .guards import DeterminismGuard
from .network import FaultConfig, FaultPlan, Network
from .ops import Log, Message, Now, Op, ProcessGen, Random, Recv, Send, Sleep, Spawn, Yield
from .rng import DeterministicRandom
from .trace import Event, Trace

__all__ = ["Simulation", "RunResult", "RunStatus", "Invariant"]


class ProcessState:
    READY = "READY"
    BLOCKED = "BLOCKED"
    SLEEPING = "SLEEPING"
    DONE = "DONE"


class RunStatus:
    COMPLETED = "completed"
    VIOLATION = "violation"
    DEADLOCK = "deadlock"
    STEP_LIMIT = "step_limit"
    ERROR = "error"


@dataclass(slots=True)
class _Process:
    """Scheduler bookkeeping for one simulated process."""

    name: str
    index: int
    """Spawn order. The only tie-breaker used anywhere, and it is an integer."""
    generator: ProcessGen
    state: str = ProcessState.READY
    mailbox: deque[Message] = field(default_factory=deque)
    wake_at: Optional[int] = None
    awaiting_recv: bool = False
    recv_match: Optional[Callable[[Message], bool]] = None
    """Selective-receive predicate. Messages that fail it stay in the mailbox."""
    timed_out: bool = False
    started: bool = False
    resume_value: Any = None

    def take_matching(self) -> Optional[Message]:
        """Remove and return the first mailbox message satisfying ``recv_match``.

        Scans a ``deque`` in arrival order, which is deterministic. Anything that
        does not match is left exactly where it was, so ordering guarantees for
        other message classes are preserved.
        """
        if self.recv_match is None:
            return self.mailbox.popleft() if self.mailbox else None
        for position, message in enumerate(self.mailbox):
            if self.recv_match(message):
                del self.mailbox[position]
                return message
        return None

    def has_matching(self) -> bool:
        if self.recv_match is None:
            return bool(self.mailbox)
        return any(self.recv_match(message) for message in self.mailbox)


@dataclass(frozen=True, slots=True)
class Invariant:
    """A named property that must hold after every scheduler step.

    ``predicate`` takes no arguments and closes over whatever state it cares
    about. ``describe`` is optional and is called only on failure, to put the
    offending numbers into the bug report -- there is no point paying for string
    formatting on the millions of steps where nothing is wrong.
    """

    name: str
    predicate: Callable[[], bool]
    describe: Optional[Callable[[], str]] = None


@dataclass(slots=True)
class RunResult:
    """Everything one simulation run produced. The unit of evidence."""

    seed: int
    status: str
    trace: Trace
    steps: int
    end_time_micros: int
    fault_plan: FaultPlan
    """Faults that actually fired. In sample mode this is the recording; in plan
    mode it is the plan that was supplied."""
    violation: Optional[InvariantViolation] = None
    error: Optional[BaseException] = None

    @property
    def failed(self) -> bool:
        """True for any outcome that is a finding rather than a clean run."""
        return self.status != RunStatus.COMPLETED

    @property
    def violated(self) -> bool:
        return self.status == RunStatus.VIOLATION

    def digest(self) -> str:
        return self.trace.digest()

    def raise_if_failed(self) -> "RunResult":
        if self.violation is not None:
            raise self.violation
        if self.error is not None:
            raise self.error
        return self

    def summary(self) -> str:
        return (
            f"seed={self.seed} status={self.status} steps={self.steps} "
            f"events={len(self.trace)} faults={len(self.fault_plan)} "
            f"t={to_seconds(self.end_time_micros):.6f}s digest={self.trace.short_digest()}"
        )


class Simulation:
    """One deterministic run of a concurrent system, derived entirely from a seed.

    Typical use::

        sim = Simulation(seed=1234, fault_config=FaultConfig.realistic())
        sim.spawn("producer", producer())
        sim.invariant("no double credit", lambda: store.total <= expected)
        result = sim.run()

    The object is single-use. A second ``run()`` raises, because a half-consumed
    generator cannot be rewound and silently continuing would produce a result
    that the seed does not describe.
    """

    def __init__(
        self,
        seed: int,
        *,
        fault_config: Optional[FaultConfig] = None,
        fault_plan: Optional[FaultPlan] = None,
        max_steps: int = 200_000,
        max_time: Optional[float] = None,
        strict: bool = True,
    ) -> None:
        self.seed = int(seed)
        self.clock = VirtualClock()
        self.trace = Trace()

        # One root generator, three named sub-streams. Separation means adding a
        # fault decision does not shift subsequent scheduling decisions, which
        # keeps shrinking far less noisy than it would otherwise be.
        self._root_rng = DeterministicRandom(self.seed, "root")
        self._sched_rng = self._root_rng.substream("schedule")
        self._fault_rng = self._root_rng.substream("fault")
        self._app_rng = self._root_rng.substream("app")

        self._config = fault_config if fault_config is not None else FaultConfig.none()
        self._network = Network(self._fault_rng, self._config, fault_plan)
        self._supplied_plan = fault_plan

        self._processes: dict[str, _Process] = {}
        self._inflight: list[Message] = []
        self._invariants: list[Invariant] = []

        self._step = 0
        self._max_steps = max_steps
        self._max_time_micros = to_micros(max_time) if max_time is not None else None
        self._strict = strict
        self._consumed = False

    # -- construction ----------------------------------------------------

    def spawn(self, name: str, generator: ProcessGen) -> None:
        """Register a process. Names must be unique: they are its identity in the
        trace, in mailbox routing, and in every bug report."""
        if name in self._processes:
            raise ValueError(f"duplicate process name {name!r}")
        process = _Process(name=name, index=len(self._processes), generator=generator)
        self._processes[name] = process
        self._record("SPAWN", name, {})

    def invariant(
        self,
        name: str,
        predicate: Callable[[], bool],
        describe: Optional[Callable[[], str]] = None,
    ) -> None:
        """Declare a property checked after every single scheduler step.

        Checking after *every* step, rather than at the end, is what turns a
        vague "the totals came out wrong" into "at step 47, immediately after
        worker-b delivered message 12, the total exceeded the expected value".
        """
        self._invariants.append(Invariant(name, predicate, describe))

    # -- the loop --------------------------------------------------------

    def run(self) -> RunResult:
        """Run to completion, deadlock, violation, or step limit.

        Never raises for a *finding*: findings are returned in the
        :class:`RunResult` so that a search over ten thousand seeds is a loop and
        not an exercise in exception plumbing. Call ``raise_if_failed()`` if you
        want the exception.
        """
        if self._consumed:
            raise RuntimeError(
                "a Simulation is single-use; construct a new one (the generators "
                "of a finished run cannot be rewound, and pretending otherwise "
                "would produce a result the seed does not describe)"
            )
        self._consumed = True

        guard = DeterminismGuard(strict=self._strict)
        try:
            with guard:
                return self._loop()
        except (InvariantViolation, Deadlock, StepLimitExceeded):
            raise  # already converted to a RunResult inside _loop; defensive only
        except BaseException as exc:  # noqa: BLE001 - we want the evidence, whatever it is
            return self._result(RunStatus.ERROR, error=exc)

    def _loop(self) -> RunResult:
        try:
            self._check_invariants()
        except InvariantViolation as violation:
            return self._result(RunStatus.VIOLATION, violation=violation)

        while True:
            ready = [p for p in self._processes.values() if p.state == ProcessState.READY]
            deliverable = [
                index
                for index, message in enumerate(self._inflight)
                if message.deliver_at <= self.clock.now
            ]

            if not ready and not deliverable:
                outcome = self._advance_time()
                if outcome is not None:
                    return outcome
                continue

            self._step += 1
            if self._step > self._max_steps:
                return self._result(
                    RunStatus.STEP_LIMIT,
                    error=StepLimitExceeded(
                        f"exceeded {self._max_steps} scheduler steps",
                        seed=self.seed,
                        step=self._step,
                        trace=self.trace,
                    ),
                )

            # Build the enabled set in a fixed order, *then* draw. The RNG must
            # never be asked to choose from a list whose construction order could
            # itself vary -- that is how "deterministic" frameworks quietly stop
            # being deterministic.
            transitions: list[tuple[str, int]] = [
                ("P", position) for position in range(len(ready))
            ]
            transitions.extend(("M", index) for index in deliverable)
            kind, target = self._sched_rng.choice(transitions)

            if kind == "P":
                self._advance_process(ready[target])
            else:
                self._deliver(target)

            try:
                self._check_invariants()
            except InvariantViolation as violation:
                return self._result(RunStatus.VIOLATION, violation=violation)

            if (
                self._max_time_micros is not None
                and self.clock.now > self._max_time_micros
            ):
                return self._result(RunStatus.COMPLETED)

    def _advance_time(self) -> Optional[RunResult]:
        """Nothing is enabled. Jump to the next scheduled instant, or stop.

        Returns a :class:`RunResult` if the run is over, otherwise ``None``.
        """
        candidates: list[int] = [m.deliver_at for m in self._inflight]
        candidates.extend(
            p.wake_at for p in self._processes.values() if p.wake_at is not None
        )

        if not candidates:
            alive = [p for p in self._processes.values() if p.state != ProcessState.DONE]
            if not alive:
                return self._result(RunStatus.COMPLETED)
            waiting = ", ".join(p.name for p in alive)
            return self._result(
                RunStatus.DEADLOCK,
                error=Deadlock(
                    f"no process can run, nothing is in flight, no timer will fire; "
                    f"still waiting: {waiting}",
                    seed=self.seed,
                    step=self._step,
                    trace=self.trace,
                ),
            )

        target = min(candidates)
        if self._max_time_micros is not None and target > self._max_time_micros:
            return self._result(RunStatus.COMPLETED)
        self.clock.advance_to(target)

        # Wake in spawn order. Deterministic, and the scheduler will immediately
        # randomise which of the woken processes actually runs first.
        for process in self._processes.values():
            if process.wake_at is None or process.wake_at > self.clock.now:
                continue
            if process.state == ProcessState.SLEEPING:
                process.state = ProcessState.READY
                process.wake_at = None
                self._record("WAKE", process.name, {})
            elif process.state == ProcessState.BLOCKED:
                process.state = ProcessState.READY
                process.wake_at = None
                process.timed_out = True
        return None

    # -- transitions -----------------------------------------------------

    def _advance_process(self, process: _Process) -> None:
        """Resume one process until its next yield."""
        if process.awaiting_recv:
            process.awaiting_recv = False
            message = process.take_matching()
            process.recv_match = None
            process.timed_out = False
            if message is not None:
                process.resume_value = message
                self._record("RECV", process.name, message.summary())
            else:
                process.resume_value = None
                self._record("TIMEOUT", process.name, {})

        try:
            if not process.started:
                process.started = True
                operation = process.generator.send(None)
            else:
                operation = process.generator.send(process.resume_value)
        except StopIteration:
            process.state = ProcessState.DONE
            process.wake_at = None
            self._record("EXIT", process.name, {})
            return

        process.resume_value = None
        self._handle(process, operation)

    def _handle(self, process: _Process, operation: Op) -> None:
        if isinstance(operation, Send):
            self._do_send(process, operation)
        elif isinstance(operation, Recv):
            self._do_recv(process, operation)
        elif isinstance(operation, Sleep):
            micros = to_micros(operation.duration)
            process.state = ProcessState.SLEEPING
            process.wake_at = self.clock.now + micros
            self._record("SLEEP", process.name, {"for_us": micros})
        elif isinstance(operation, Now):
            process.resume_value = self.clock.now
        elif isinstance(operation, Random):
            process.resume_value = self._app_rng.unit()
        elif isinstance(operation, Yield):
            self._record("YIELD", process.name, {})
        elif isinstance(operation, Log):
            self._record("LOG", process.name, {"label": operation.label, **(operation.detail or {})})
        elif isinstance(operation, Spawn):
            self.spawn(operation.name, operation.generator)
        else:
            raise TypeError(
                f"process {process.name!r} yielded {operation!r}, which is not a deterministic_testing Op. "
                f"Processes may only yield Send/Recv/Sleep/Now/Random/Yield/Log/Spawn."
            )

    def _do_send(self, process: _Process, operation: Send) -> None:
        seq = self._network.next_seq()
        copies, faults = self._network.dispatch(
            seq=seq,
            sender=process.name,
            recipient=operation.recipient,
            payload=operation.payload,
            now=self.clock.now,
        )
        self._record(
            "SEND",
            process.name,
            {"seq": seq, "to": operation.recipient, "payload": operation.payload},
        )
        for fault in faults:
            self._record(fault.kind, process.name, {"seq": seq, "value": fault.value})
        self._inflight.extend(copies)

    def _do_recv(self, process: _Process, operation: Recv) -> None:
        """Arm a receive.

        Note that this always costs one extra scheduler step, even when a message
        is already waiting. That is deliberate: it makes "check the mailbox" an
        interleaving point, which is where a surprising number of real
        check-then-act bugs live.
        """
        process.awaiting_recv = True
        process.recv_match = operation.match
        process.timed_out = False
        if process.has_matching():
            process.state = ProcessState.READY
            process.wake_at = None
        else:
            process.state = ProcessState.BLOCKED
            process.wake_at = (
                self.clock.now + to_micros(operation.timeout)
                if operation.timeout is not None
                else None
            )

    def _deliver(self, index: int) -> None:
        message = self._inflight.pop(index)
        recipient = self._processes.get(message.recipient)
        if recipient is None or recipient.state == ProcessState.DONE:
            self._record(
                "UNROUTABLE",
                message.sender,
                {"seq": message.seq, "to": message.recipient},
            )
            return
        recipient.mailbox.append(message)
        self._record("DELIVER", message.recipient, message.summary())
        # Only unblock if this arrival is one the recipient is actually waiting
        # for. A process selectively awaiting a database reply must not be woken
        # by an unrelated job landing in its mailbox: it would run, find nothing
        # matching, and report a spurious timeout.
        if recipient.state == ProcessState.BLOCKED and recipient.has_matching():
            recipient.state = ProcessState.READY
            recipient.wake_at = None

    # -- checking and reporting ------------------------------------------

    def _check_invariants(self) -> None:
        for invariant in self._invariants:
            try:
                held = bool(invariant.predicate())
            except Exception as exc:  # noqa: BLE001 - a throwing invariant is a violation
                raise InvariantViolation(
                    invariant.name,
                    seed=self.seed,
                    step=self._step,
                    detail=f"predicate raised {type(exc).__name__}: {exc}",
                    trace=self.trace,
                ) from exc
            if not held:
                detail = invariant.describe() if invariant.describe else ""
                self._record("VIOLATION", "invariant", {"invariant": invariant.name, "detail": detail})
                raise InvariantViolation(
                    invariant.name,
                    seed=self.seed,
                    step=self._step,
                    detail=detail,
                    trace=self.trace,
                )

    def _record(self, kind: str, process: str, detail: dict[str, Any]) -> None:
        self.trace.record(
            Event(
                step=self._step,
                time_micros=self.clock.now,
                kind=kind,
                process=process,
                detail=detail,
            )
        )

    def _result(
        self,
        status: str,
        *,
        violation: Optional[InvariantViolation] = None,
        error: Optional[BaseException] = None,
    ) -> RunResult:
        plan = self._supplied_plan if self._supplied_plan is not None else self._network.recorded_plan()
        return RunResult(
            seed=self.seed,
            status=status,
            trace=self.trace,
            steps=self._step,
            end_time_micros=self.clock.now,
            fault_plan=plan,
            violation=violation,
            error=error,
        )

    # -- introspection ---------------------------------------------------

    @property
    def scheduling_draws(self) -> int:
        """How many raw draws the scheduling stream consumed. A determinism canary."""
        return self._sched_rng.draws
