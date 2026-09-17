# FAILURES.md

An honest log of what went wrong, or nearly went wrong, while building `deterministic-testing`,
written as it happened.

The entries that matter most are the ones about non-determinism leaking into the
framework itself, because that is precisely the bug class the framework exists to
catch. A deterministic simulator that is quietly non-deterministic is worse than
no simulator at all: it hands out seeds that do not reproduce, and it destroys
trust in every result it has ever reported.

---

## 1. The headline finding: the obvious determinism test does not work

The single most important test in this project was supposed to be "run the same
seed 100 times, assert the trace hash is identical every time". I wrote it, it
passed, and I did not believe it. So I mutation-tested it: I took a copy of the
repository, deliberately introduced the most likely real non-determinism bug, and
checked whether the test caught it.

**Mutant A.** In `deterministic_testing/simulation.py`, the scheduler builds the list of runnable
processes each step:

```python
ready = [p for p in self._processes.values() if p.state == ProcessState.READY]
```

I replaced it with a version that round-trips through a `set` of process names —
exactly the kind of thing you write without thinking:

```python
ready = [self._processes[n] for n in {p.name for p in self._processes.values()
                                      if p.state == ProcessState.READY}]
```

Result:

| test | mutant A |
| --- | --- |
| same seed, 100 runs, identical digest (in-process) | **passed** |
| same seed, 4 workload sizes, 2 fault profiles, 3 variants (in-process) | **passed** |
| identical across separate OS processes with varying `PYTHONHASHSEED` | **failed, all 5 parameters** |

The in-process test is blind to it. Python computes the hash salt for `str` once
per interpreter start, so within a single process a set of strings iterates in a
stable — but arbitrary — order. Running the same seed a thousand times in one
process cannot see the problem. Only a subprocess with a different salt can.

**Mutant B.** Same experiment with `derive_seed` in `deterministic_testing/rng.py` rewritten to
use the builtin `hash()` instead of SHA-256. Identical outcome: the in-process
100x test passed, the cross-process test failed, and so did the pinned-value
assertion in `tests/test_core.py::test_substream_seeds_are_not_process_dependent`.

**What I changed as a result.** The determinism proof is now two tests, not one.
The 100x digest test stays, because it catches ordinary state leakage between
runs. The load-bearing one is
`test_identical_across_processes_with_different_hash_seeds`, which shells out to
fresh interpreters with `PYTHONHASHSEED` set to `0`, `1`, `424242` and `random`
twice, and requires every one of them to agree with each other *and* with the
parent process.

**What I did not do.** I did not set `PYTHONHASHSEED=0` in `pyproject.toml` or in
CI. It would make the suite green and would defeat the only test that works.
There is a comment saying so in both files, because it is exactly the sort of
"fix" a future maintainer applies at 6pm on a Friday.

The general lesson, and the one I would lead with if asked about this project:
*a test that cannot fail is not evidence.* I only know the determinism proof works
because I watched it fail against a bug I planted on purpose.

---

## 2. The framework found a bug I did not plant, in my own fix

The planted bug was in `worker_buggy`: check the dedup table, apply the side
effect, record the key. The window between the check and the record is what
allows a redelivered message to double-count. `deterministic-testing` found it at seed 1.

I then wrote `worker_claim`, which replaces check-and-mark with a single atomic
`claim` on the store. That closes the window, and it does fix the planted bug. I
expected a clean sweep.

Over 10,000 seeds the claim version failed 1,158 times.

The shrunk trace for seed 11 says exactly what happened, and it was not something
I had thought about:

1. `worker-a` claims `item-3` and wins.
2. The network **duplicates** the `claim_reply(won=True)`. One copy is consumed;
   the second sits in `worker-a`'s mailbox.
3. `worker-a` credits, acks — and the ack is **delayed** past the retry timeout.
4. The broker redelivers `item-3`, and the load balancer sends it to `worker-a`.
5. `worker-a` sends a fresh `claim` and waits for the reply. Its selective receive
   matches on `(op, key)`, so it immediately consumes the **stale duplicate** from
   step 2, which says `won=True`.
6. `worker-a` credits `item-3` a second time.

The atomic claim was correct. The reply *correlation* was not: `(op, key)` cannot
distinguish the answer to this request from a duplicated answer to the last one.
It is the classic missing-request-id bug, and I would not have found it by
reading the code, because when I wrote `_is_reply(op, key)` it looked obviously
sufficient.

Two things about this are worth saying plainly:

* This is the more interesting of the two bugs, and I did not put it there.
* The intermediate fix reduced the failure rate from 85.0% to 11.6% of seeds. In
  production that is indistinguishable from a fix. The bug would have gone from
  "happens most days" to "happens about weekly", which is precisely the regime
  where people stop investigating and start adding a reconciliation job.

The final `worker_fixed` makes the store operation a single idempotent `apply`
*and* correlates replies by a per-request id. 10,000 seeds, no violations.

---

## 3. Things I got wrong along the way

**Bash heredocs mangled the first version of `clock.py` and `trace.py`.** The
shell failed to parse a multi-line quoted heredoc containing backticks and
quotes, and silently wrote nothing for the second and third files in the chain.
Caught immediately by listing the directory. Noted here only because "the command
reported no error and produced no file" is a reminder to verify writes rather
than assume them.

**`store` initially kept its state in a module-level attribute** (`store.state`),
set by `build_pipeline`. That works until you construct two simulations in one
interpreter, at which point run N depends on run N−1 — hidden coupling of exactly
the kind this project is about. Changed to pass the state in as a parameter
before it ever bit me. `tests/test_determinism.py::test_runner_is_pure_across_calls`
now interleaves unrelated runs between two runs of the same scenario and requires
identical digests, so a regression here fails loudly.

**Selective receive was not in the original design, and the model was wrong
without it.** With a single mailbox per process, a worker waiting for a database
reply would happily consume the next job the broker sent it. That produced
spurious timeouts that had nothing to do with the bug under investigation. Adding
an Erlang-style `match` predicate to `Recv` fixed the model. It also turned out to
be load-bearing for bug #2 above: the bug is *precisely* that the match predicate
is too loose.

**`Recv` costs a scheduler step even when a message is already waiting.** This
was not deliberate at first — it fell out of unifying the "mailbox has something"
and "mailbox is empty" paths. Having noticed it, I kept it, because it makes
"check the mailbox" an interleaving point, and that is where check-then-act bugs
live. It is documented as intentional in `_do_recv`.

---

## 4. What I expected to go wrong and did not

I expected the determinism guard — which replaces `time.time`, `random.random`,
`uuid.uuid4` and friends with functions that raise — to break `pytest` itself,
since plenty of infrastructure reads the clock. It did not, because the guard is
installed only for the duration of `Simulation.run()` and removed in a `finally`,
and pytest does its timing outside the test body. Had it broken, the fallback
would have been to inspect the calling frame and exempt stdlib and site-packages;
that was not needed, so the guard is strictly stronger than planned.

I also expected float time to cause trouble, and pre-empted it by making virtual
time an integer count of microseconds. `test_micros_conversion_is_exact_for_repeated_addition`
records why: ten additions of `to_micros(0.1)` is exactly `to_micros(1.0)`, which
is not true of the float version.

---

## 5. Known-honest limitations

These are not failures so much as the boundary of the claim, and they belong here
rather than being discovered by an interviewer.

* **Shrinking is a search, not a proof of minimality.** Removing a fault changes
  how many messages the run produces, so sequence numbers after that point no
  longer refer to the same messages. Every candidate is genuinely re-run and
  re-checked, so the result definitely reproduces the bug — but a smaller
  scenario may exist and not have been found. For seed 11 on the `claim` variant,
  shrinking only got 141 events down to 139 and could not reduce the workload at
  all; for seed 1 on the `buggy` variant it got 91 events and 9 faults down to 43
  events and 1 fault. The tool is much better on some inputs than others, and the
  report says which.
* **The interleaving space is explored only at yield points.** A genuine data race
  on shared memory between two yields is invisible to this scheduler, by
  construction. See DESIGN.md.
* **The determinism guard is a Python-level patch, not a sandbox.** Code that
  bound a reference before the guard installed (`from time import time` at import
  time) slips through, as does anything in a C extension. It raises the cost of a
  mistake from zero to high; it does not make one impossible.
