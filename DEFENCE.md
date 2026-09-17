# DEFENCE.md

The design arguments behind `deterministic-testing`, the costs I accepted, and the questions I
expect to be asked.

---

## 1. How is this different from just running the tests a thousand times?

Running a flaky test repeatedly gives you *more samples from the same
distribution*. The distribution is set by the OS scheduler and by whatever your
machine happened to be doing, and you have no control over either. Three things
follow, and all three are fatal:

**You cannot steer it.** A retry timeout of 500 ms fires only when an
acknowledgement is late by more than 500 ms. On a loopback socket that essentially
never happens, so a thousand runs sample the same narrow region of behaviour a
thousand times. `deterministic-testing` sets the delay from the seed, so the interesting region is
reachable — and reachable *cheaply*, because the delay is virtual.

**You cannot replay it.** Suppose run 617 of 1,000 fails. You have a stack trace
and nothing else. You cannot re-run 617. This is the difference between a bug you
can work on and a bug you can only wait for. With `deterministic-testing`, run 617 *is* seed 617,
and it reproduces on any machine, forever.

**You cannot shrink it.** Even if run 617 failed reproducibly, you would have the
whole thing — every message, every retry. `deterministic-testing` re-runs candidate scenarios and
reduces seed 1 from 91 events and 9 faults to 43 events and a single dropped
message. That reduction is only possible because re-running is exact.

There is a fourth, quieter point. A thousand green runs is weak evidence, and it
feels like strong evidence. `tests/test_pipeline.py` contains a test that runs the
buggy pipeline 1,000 consecutive times and passes every one. That test is not a
strawman: it varies the seed, so it varies the interleaving, and it asserts the
right property. It is simply testing a system whose network never misbehaves.

---

## 2. Why generators and not threads? What does that cost you?

**The argument for.** Determinism requires that the framework, not the OS, decides
who runs next. With OS threads you cannot make that decision, cannot observe the
one that was made, and cannot reproduce it. You could try to force it — a global
lock handed round in a seeded order — but you would still be at the mercy of the
GIL's switch interval, of I/O, and of the fact that a thread can be preempted
between any two bytecodes. You would end up with something slower than generators
and still not deterministic. If the guarantee is "the seed fully describes the
run", threads cannot provide it.

Generators also make the interleaving points *explicit and visible in the source*.
When you read the worker in `examples/pipeline.py`, every `yield` is a place where
the world may change underneath you. That is valuable documentation in its own
right.

**The cost, stated plainly.** A process runs uninterrupted between one `yield` and
the next. If two processes mutate a shared list in that gap, `deterministic-testing` will never
interleave them, and **a genuine data race on shared memory is invisible to this
tool by construction.**

So: right tool for message passing, timeouts, retries, idempotency, leader
election, distributed state machines. Wrong tool for memory races — for those you
want a thread sanitiser or a model checker over the memory model, and I would say
so rather than pretending the coverage is complete.

There is a second, subtler cost: *you choose where the yields go*. Put too few in
and you hide bugs. This is real, and it is why I would describe the tool as
raising the ceiling on what you can test rather than removing the need to think.

---

## 3. How does the virtual clock work, and why does it matter so much?

Time is an integer count of microseconds in `deterministic_testing/clock.py`. The only way it
moves is `advance_to()`, and the only caller is the scheduler, which invokes it in
exactly one situation: nothing is enabled — no process can run and no message has
reached its delivery time. It then jumps directly to the earliest instant at which
something becomes enabled (the next timer expiry or the next delivery).

Consequences:

* **Timeouts are free.** The example pipeline shuts down via a 30-second idle
  timeout. Measured: 10,000 seeds of the corrected pipeline take 21 seconds of
  wall clock and cover 89 hours of simulated time -- a speedup of about 15,000x.
  Without virtual time, that experiment would take those 89 hours.
* **Timing races become schedulable.** "The ack arrived 1 µs after the timeout
  fired" is a decision drawn from the seed, not a coincidence you wait for.
* **No test ever sleeps**, so there is no flakiness from a loaded CI machine.

Why integers rather than floats: `0.1 + 0.2 != 0.3`. That error is deterministic,
so it would not break replay, but it makes traces ugly and equality comparisons
fragile. Conversion from seconds happens exactly once, at the API boundary; every
operation after that is exact integer arithmetic. There is a test for this
(`test_micros_conversion_is_exact_for_repeated_addition`).

---

## 4. How does shrinking work?

Two dimensions, alternated until neither improves.

The network runs in one of two modes. In **sample mode** it draws fault decisions
from the seeded fault stream and *records* every one into a `FaultPlan` keyed by
message sequence number. In **plan mode** it consults a supplied plan and touches
no randomness at all. So a failing run can be re-run with an arbitrary subset of
its own faults.

1. **Workload.** Try smaller item counts, take the smallest that still fails.
   Done first, because fewer items means fewer messages and a smaller space for
   step 2.
2. **Faults.** Delta debugging (Zeller's `ddmin`) over the recorded plan: halve,
   test each half, test each complement, refine granularity.

Two details that matter more than the algorithm:

* Every candidate must violate the **same named invariant**. Without that check
  the shrinker wanders into a different bug and confidently reports a minimal
  reproduction of something you were not looking for.
* **It is a search, not a proof.** Removing a fault changes how many messages the
  run produces, so sequence numbers after the removal point no longer name the
  same messages. Each candidate is genuinely re-run and re-checked, so the result
  definitely reproduces — but a smaller scenario may exist and not have been
  found. Seed 1 went from 91 events to 43. Seed 11 on the `claim` variant went
  from 141 to 139, and the workload would not reduce at all. I would not claim
  minimality, and the report does not.

---

## 5. What can this framework *not* find?

* **Data races on shared memory between yield points.** By construction. See Q2.
* **Anything you did not model.** The simulation exercises your generators, not
  your service. If they diverge, you have verified fiction.
* **Anything you did not assert.** No invariant, no violation. `deterministic-testing` has no
  opinion about whether your business logic is correct.
* **Bugs needing an interleaving no seed reaches.** The search is random, not
  exhaustive. It is a sampler over a combinatorial space, and absence of evidence
  after 10,000 seeds is weak evidence of absence. A model checker like TLA+ or
  a stateless one like Coyote can be exhaustive over a bounded state space;
  this cannot.
* **Liveness and performance properties.** The framework checks safety
  ("something bad never happens"). It detects deadlock, but it will not tell you
  that your system is eventually consistent, or fast.
* **Faults not in the model.** No disk corruption, no process crash mid-write, no
  clock skew between nodes, no Byzantine behaviour.

---

## 6. What does FoundationDB do that this does not?

FoundationDB is the reference implementation of this idea, and the gap is honest
and large.

* **They simulate the real product, not a model.** Their entire codebase is
  written in Flow, a C++ extension with actor semantics, so the production binary
  *is* single-threaded and deterministic. There is no model to drift from the
  implementation, which is the single biggest weakness of the approach here.
* **They simulate the machine, not just the network.** Disk latency, disk
  corruption, machine reboots, process kills, clock skew, whole-datacentre loss.
  `deterministic-testing` simulates a network and a scheduler.
* **They swarm-test continuously** across large fleets, accumulating simulated
  years per day, and they bias fault injection toward "buggify" points hand-placed
  in the code where the authors suspected trouble.
* **They test the tester.** They inject known bugs and confirm the simulator finds
  them within an expected time. I did a small version of this — see the mutation
  testing in FAILURES.md — but for the framework's determinism rather than for its
  bug-finding power.

The honest summary: this is a working implementation of the core idea, at a scale
where I can explain every line. It is not a competitor.

---

## 7. Ten questions I expect, with short answers

**1. Prove the determinism. How do I know the seed really is enough?**
Run `deterministic-testing verify --runs 100`, or `pytest -k determinism`. The trace digest is a
SHA-256 over every event's step, virtual timestamp, kind, process and payload —
not a summary. More importantly, the suite shells out to *separate interpreters*
with `PYTHONHASHSEED` set to `0`, `1`, `424242` and `random` twice, and requires
all of them to agree with each other and with the parent. I mutation-tested that:
a `set`-iteration bug in the scheduler passes the in-process 100x test and fails
the cross-process one. That is why both exist.

**2. Why not just fix the flaky test with a retry?**
Because the flake is the bug reporting itself. The example here double-credits a
ledger; retrying the test hides it and it reaches production. There is a real
version of that decision in the numbers: the intermediate `claim` fix reduced the
failure rate from 85% to 12% of seeds, which in production would have looked
exactly like a fix.

**3. Your scheduler picks uniformly at random. Isn't a targeted search better?**
Yes, and that is the obvious next step. PCT gives probabilistic bounds on finding
bugs of a given depth by inserting a few priority-change points; FoundationDB's
"buggify" biases toward hand-marked suspicious points. Uniform random is the
honest baseline: it needs no tuning, and it found both bugs here in the first 11
seeds.

**4. What is the performance cost of all this indirection?**
Every operation is a generator `send` plus a dict lookup plus a trace append —
about 56,000 scheduler steps per second in CPython on this machine, at a mean of
107 steps per run. But the comparison that matters is against wall-clock time, and
virtual time wins by orders of magnitude: 10,000 seeds of the corrected pipeline
cover 89 hours of simulated time in 21 seconds. The search is also embarrassingly
parallel and this implementation does not exploit that.

**5. How would I use this on a real service?**
You would not point it at your service; you would write a model of the concurrent
core as generators, keeping the real logic and replacing I/O with `Send`/`Recv`.
That is a genuine cost and the main reason such tools do not get adopted. It pays
off where the concurrency is the product — a ledger, a matching engine, a
settlement pipeline, a replication protocol — and not where the concurrency is
incidental.

**6. Why is `Recv` a scheduler step even when a message is already waiting?**
It makes "check the mailbox" an interleaving point. A surprising number of real
check-then-act bugs live exactly there. It fell out of unifying two code paths and
I kept it deliberately; it is documented in `_do_recv`.

**7. What happens if the system under test cheats and calls `time.time()`?**
It raises `NonDeterminismLeak` at that line. `deterministic_testing/guards.py` replaces
`time.time`, `monotonic`, `perf_counter`, `sleep`, the module-level `random`
functions, `uuid.uuid4` and `os.urandom` for the duration of every run, and
restores them in a `finally`. It is a Python-level patch, not a sandbox: a
reference bound at import time slips through, as does anything in C. It raises the
cost of the mistake from zero to high.

**8. Your invariants run after every step. Isn't that slow?**
Yes, and it is the right trade. Checking at the end tells you the totals are
wrong; checking every step tells you it happened at step 40, immediately after
`worker-b` credited `item-0` a second time. The framework calls `describe()` only
on failure, so the expensive string formatting is not on the hot path.

**9. How do you know the shrunk scenario is really the same bug?**
Every candidate must violate the same *named* invariant, not merely fail. And the
minimal scenario is re-run from its own serialised description — the test
`test_shrunk_scenario_replays_identically_many_times` replays it 30 times and
requires one digest. What I do not claim is minimality: shrinking is a search over
configurations, because removing a fault shifts every subsequent message sequence
number.

**10. What would you do next?**
In order: parallelise the seed search across cores; add PCT-style biased
scheduling with a depth bound; add process-crash and restart faults, which is the
biggest missing fault class; add a causal-slice view so the shrunk trace shows
only events on the dependency chain leading to the violation, which would take
seed 1 from 43 events to about 12. Further out, the interesting question is
whether you can drive real `asyncio` code by supplying a deterministic event loop,
which would remove the model/implementation gap that is this design's main
weakness.

---

## 8. Design decisions I would defend individually

**Randomness is version-pinned.** `deterministic_testing/rng.py` derives integers and floats from
`getrandbits` using our own rejection sampling, rather than calling `randrange` or
`choice`. CPython guarantees the Mersenne Twister bit stream is stable; it does
not guarantee that `randrange` will keep consuming it the same way, and it has
already changed once. If that changed under us, every stored seed in every bug
report would silently start replaying a different scenario.

**Sub-streams are separated.** Scheduling, fault injection and application
randomness draw from independent generators derived from the root seed via
SHA-256. Adding a fault decision therefore does not shift every subsequent
scheduling decision, which keeps shrinking far less noisy.

**Sub-stream seeds use SHA-256, never `hash()`.** `hash("fault")` is salted per
process. Using it would have made two runs of the same seed diverge on the same
machine — the exact bug this project exists to catch, shipped in the foundations.

**`DeterministicRandom.choice` rejects sets at runtime.** Not a lint rule, a
`TypeError`. Set iteration order is the most likely way to break replay in Python
and the least likely to be noticed.

**The step limit is a hard error, not a truncation.** A truncated run could hide a
violation two steps away and report a clean seed. A false clean is the one outcome
worse than a false alarm.

**`PYTHONHASHSEED` is deliberately *not* pinned** in `pyproject.toml` or CI, with
a comment in both saying why. Pinning it would make the suite green and disable
the only test that catches hash-order bugs.
