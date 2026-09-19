# Heisenbug — find out *why* a test is flaky

> The test that changes when you look at it.

Every team has them: the test that fails on CI, passes when you rerun it, and
gets a `@flaky(reruns=3)` decorator so everyone can get on with their day. The
retry is not a fix. It hides a real defect — in the test, in a fixture, or in
the code under test — and it costs you a little trust in the suite every time
it fires.

Heisenbug is an agent that does the work you would do if you had the afternoon:
run the test under controlled conditions, form a hypothesis about the
nondeterminism, design an experiment that would rule it out, and keep going
until the mechanism is named.

It ends with a cause and a fix, not a retry count.

## The one question that matters first

**Does the test fail on its own?**

That single experiment splits the world in two, and almost nothing else makes
sense before you've run it:

| Observation | What it means | Where the bug is |
|---|---|---|
| Fails alone, sometimes | Carries its own nondeterminism | Unseeded randomness, the wall clock, hash ordering, a real network call |
| Passes alone, fails in the suite | Another test is breaking it | Leaked global state, an unrestored patch, a cached connection |

Most tools never ask. They count failures and retry. That's why a test which
fails in *every* CI run but passes locally gets called "broken" and sits in the
backlog for a month — when it's actually order-dependent and the fix is four
lines.

## Quickstart

```bash
pip install -e .

# The deterministic half. No API key, no model — just run the suite N times
# and see which tests disagree with themselves.
heisenbug scan examples/flaky_suite -n 5

# The investigation. Scans, then diagnoses each suspect.
export ANTHROPIC_API_KEY=sk-ant-...      # or `ant auth login`
heisenbug hunt examples/flaky_suite
```

`scan` exits 0 clean, 1 flaky, 2 if something is failing outright, so it drops
into CI on its own — you can adopt the cheap half before you spend a token.

The bundled `examples/flaky_suite` has four planted flakes of genuinely
different kinds (order-dependent, unseeded random, hash-ordering,
clock-dependent) plus one test that is simply wrong. They're there to be told
apart.

## What an investigation costs

An investigation is bounded by a **run budget** — the number of pytest
processes it may spawn, `--budget` (default 60). Tools refuse to exceed it and
tell the agent to conclude from what it has. That makes the worst case
predictable in wall-clock time and dollars; there's no runaway loop.

The expensive, well-defined search is done deterministically in Python rather
than by the model: finding which earlier test poisons a later one is a binary
search over the preceding tests, so it costs ~log₂(n) runs instead of n. On the
demo suite it pins the culprit out of 12 candidates in 5 runs. The agent
decides *when* that search is the right experiment; it doesn't drive it
step by step.

## The experiments it can run

| Tool | Question it answers |
|---|---|
| `run_in_isolation` | Does it fail on its own? |
| `find_polluting_test` | Which earlier test breaks it? (binary search) |
| `run_after` | Does this specific test break it? |
| `run_with_env` | Does the failure track `PYTHONHASHSEED` / `TZ`? |
| `read_test_source` | What module-level state is in play? |
| `search_tests` | Where does the suite touch the clock, randomness, threads? |

`run_with_env` is what separates hash ordering from plain chance: pin
`PYTHONHASHSEED` and if the outcome is *identical* within a seed but *differs*
across seeds, the "randomness" was set iteration order all along.

## Output

```
  FLAKE HUNT: FLAKY
  test_receipt_total_is_usd is broken by an earlier test, not by itself.

  test_b_receipt.py::test_receipt_total_is_usd
    cause      depends on test order  (high confidence)
    frequency  0/2 alone, fails after the polluter
    culprit    test_a_checkout.py::test_eu_checkout_uses_euro_symbol
    evidence   Passed 2/2 in isolation; bisect narrowed 12 preceding tests to one.
    reproduce  pytest test_a_checkout.py::test_eu_checkout_uses_euro_symbol test_b_receipt.py::test_receipt_total_is_usd
    fix        Restore cart.SETTINGS['currency'] in an autouse fixture.
```

`--report out.json` writes the same thing as JSON. Exit code is 0 / 1 / 2 for
clean / flaky / broken.

The agent is instructed that "undetermined, and here's what I ruled out" is a
valid and useful answer — a confident wrong cause is worse than an honest
negative, because someone will act on it.

## How it's built

- Python, official `anthropic` SDK. The investigation loop is the SDK **tool
  runner** with `@beta_tool` functions; the final report comes from
  **structured outputs** (`client.messages.parse` + a Pydantic schema).
- Model: `claude-opus-5`, adaptive thinking, high effort.
- Results are read back through a small pytest plugin
  (`heisenbug/_report_plugin.py`) rather than JUnit XML, because an experiment
  is only useful if the node id it reports is one you can feed back on a
  command line. JUnit XML can't always reproduce one exactly.
- Every run is a fresh subprocess with the repo's own default flags neutralized
  (`-o addopts=`), so an experiment measures the test rather than the config.

## Limits

- pytest only, and the tests must be runnable by the same interpreter
  Heisenbug is installed in.
- Order experiments pass explicit node ids on the command line; pytest's
  class/module grouping can reorder them in unusual suites.
- Concurrency flakes (`pytest-xdist`, threads) are detected but rarely
  root-caused — a race that needs a specific interleaving may not reproduce
  within the budget. The agent is told to report that honestly rather than
  guess.
