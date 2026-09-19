"""Tests for the parts that must work without calling the API.

These assert only on deterministic behaviour. The demo suite's random, clock,
and hash-order flakes are genuinely nondeterministic, so asserting that a scan
detects them on a given run would make *this* suite flaky — which would be an
embarrassing way to ship a flaky-test hunter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from heisenbug.report import FlakeReport, render_report
from heisenbug.runner import (
    BudgetExhausted,
    PytestRunner,
    RunBudget,
    bisect_polluter,
)
from heisenbug.scan import Stability, scan
from heisenbug.tools import HuntContext, build_tools

DEMO = Path(__file__).resolve().parent.parent / "examples" / "flaky_suite"

VICTIM = "test_b_receipt.py::test_receipt_total_is_usd"
POLLUTER = "test_a_checkout.py::test_eu_checkout_uses_euro_symbol"
BROKEN = "test_c_promos.py::test_discount_rounds_to_nearest_cent"
HASH_FLAKE = "test_c_promos.py::test_first_tag_is_clearance"
STABLE = "test_a_checkout.py::test_line_total_single_item"


@pytest.fixture
def runner() -> PytestRunner:
    return PytestRunner(DEMO, budget=RunBudget(limit=40))


def _tools(runner: PytestRunner, **kwargs) -> dict:
    all_tests, _ = runner.collect()
    ctx = HuntContext(repo_root=DEMO, runner=runner, all_tests=all_tests, **kwargs)
    return {t.name: t for t in build_tools(ctx)}


# --- runner -----------------------------------------------------------------


def test_collect_returns_real_node_ids(runner: PytestRunner):
    tests, _ = runner.collect()
    assert VICTIM in tests
    assert all("::" in nid for nid in tests)
    assert not any(nid.endswith(".py") for nid in tests)


def test_node_ids_from_a_run_are_runnable(runner: PytestRunner):
    """A reported node id must be one pytest will accept back."""
    result = runner.run([STABLE])
    assert list(result.outcomes) == [STABLE]
    assert result.outcome_of(STABLE) == "passed"


def test_budget_is_enforced():
    runner = PytestRunner(DEMO, budget=RunBudget(limit=2))
    runner.run([STABLE])
    runner.run([STABLE])
    with pytest.raises(BudgetExhausted):
        runner.run([STABLE])


def test_malformed_node_id_is_reported_not_raised(runner: PytestRunner):
    result = runner.run(["test_nope.py::test_missing"])
    assert result.collection_error


# --- the central distinction ------------------------------------------------


def test_order_dependent_test_passes_alone(runner: PytestRunner):
    """The victim fails in every full-suite run, but is not actually broken."""
    assert runner.run([VICTIM]).outcome_of(VICTIM) == "passed"


def test_order_dependent_test_fails_after_its_polluter(runner: PytestRunner):
    result = runner.run([POLLUTER, VICTIM])
    assert result.failed(VICTIM)


def test_genuinely_broken_test_fails_alone_too(runner: PytestRunner):
    """The distinction that keeps the agent from bisecting a real bug."""
    assert runner.run([BROKEN]).failed(BROKEN)


def test_bisect_isolates_the_single_polluter(runner: PytestRunner):
    all_tests, _ = runner.collect()
    before = all_tests[: all_tests.index(VICTIM)]
    prefix, runs, note = bisect_polluter(runner, VICTIM, before)
    assert prefix == [POLLUTER]
    assert runs < len(before)  # cheaper than trying them one at a time
    assert "single polluting test" in note


def test_bisect_reports_when_nothing_reproduces(runner: PytestRunner):
    prefix, _runs, note = bisect_polluter(runner, STABLE, [POLLUTER])
    assert prefix == []
    assert "not reproducible" in note


# --- hash ordering ----------------------------------------------------------


def test_hash_seed_makes_the_hash_flake_deterministic(runner: PytestRunner):
    """Same seed, same outcome — the signature that separates hash order from chance."""
    outcomes = {
        seed: {runner.run([HASH_FLAKE], env={"PYTHONHASHSEED": seed}).outcome_of(HASH_FLAKE)
               for _ in range(2)}
        for seed in ("0", "1")
    }
    for seed, results in outcomes.items():
        assert len(results) == 1, f"seed {seed} gave mixed outcomes: {results}"
    assert outcomes["0"] != outcomes["1"], "expected the seed to change the outcome"


# --- tools ------------------------------------------------------------------


def test_isolation_tool_explains_a_pass(runner: PytestRunner):
    report = _tools(runner)["run_in_isolation"].call({"test": VICTIM, "repeat": 2})
    assert "0 failed" in report
    assert "Bisect" in report or "bisect" in report


def test_find_polluting_test_names_the_culprit(runner: PytestRunner):
    report = _tools(runner)["find_polluting_test"].call({"test": VICTIM})
    assert POLLUTER in report
    assert "pytest" in report  # includes a reproducer command


def test_find_polluting_test_handles_the_first_test(runner: PytestRunner):
    report = _tools(runner)["find_polluting_test"].call({"test": STABLE})
    assert "nothing precedes it" in report


def test_read_test_source_surfaces_module_state(runner: PytestRunner):
    report = _tools(runner)["read_test_source"].call({"test": POLLUTER})
    assert "SETTINGS" in report  # the mutation that causes the pollution
    assert "def test_eu_checkout_uses_euro_symbol" in report


def test_read_test_source_refuses_paths_outside_the_repo(runner: PytestRunner):
    report = _tools(runner)["read_test_source"].call({"test": "../../etc/passwd::x"})
    assert report.startswith("ERROR")


def test_search_tests_finds_nondeterminism(runner: PytestRunner):
    report = _tools(runner)["search_tests"].call({"pattern": r"random\.|datetime\.now"})
    assert "cart.py" in report


def test_search_tests_rejects_a_bad_regex(runner: PytestRunner):
    report = _tools(runner)["search_tests"].call({"pattern": "([unclosed"})
    assert "invalid regex" in report


def test_tools_refuse_to_exceed_the_budget():
    runner = PytestRunner(DEMO, budget=RunBudget(limit=1))
    tools = _tools(runner)
    tools["run_in_isolation"].call({"test": STABLE, "repeat": 1})
    assert "BUDGET" in tools["run_in_isolation"].call({"test": STABLE, "repeat": 3})


# --- scan -------------------------------------------------------------------


def test_scan_separates_stable_from_failing():
    result = scan(DEMO, repeats=2)
    assert result.stability[STABLE].status == "stable"
    assert BROKEN in {s.node_id for s in result.always_failing}
    # The order-dependent victim fails in every full-suite run, so a scan alone
    # cannot tell it from a real bug. That is precisely what the agent is for.
    assert VICTIM in {s.node_id for s in result.always_failing}


def test_stability_classification():
    assert Stability("t", passed=3, failed=2).status == "intermittent"
    assert Stability("t", failed=5).status == "always-failing"
    assert Stability("t", passed=5).status == "stable"
    assert Stability("t", passed=4, missing=1).status == "sometimes-not-run"


# --- report -----------------------------------------------------------------


def test_exit_code_tracks_verdict():
    def report(verdict: str) -> FlakeReport:
        return FlakeReport(verdict=verdict, headline="h", tests_examined=0)

    assert report("CLEAN").exit_code == 0
    assert report("FLAKY").exit_code == 1
    assert report("BROKEN").exit_code == 2


def test_render_report_shows_cause_and_culprit():
    report = FlakeReport(
        verdict="FLAKY",
        headline="One test depends on another's leftovers.",
        tests_examined=1,
        diagnoses=[
            {
                "test": VICTIM,
                "cause": "order_dependent",
                "confidence": "high",
                "failure_rate": "5/5 suite runs, 0/3 alone",
                "culprit": POLLUTER,
                "evidence": "Bisected 12 preceding tests in 5 runs.",
                "reproducer": f"pytest {POLLUTER} {VICTIM}",
                "fix": "Restore cart.SETTINGS in a fixture.",
            }
        ],
    )
    text = render_report(report, runs_used=14)
    assert "depends on test order" in text
    assert POLLUTER in text
    assert "14 pytest runs used" in text
