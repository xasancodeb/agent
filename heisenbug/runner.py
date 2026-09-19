"""Running pytest under controlled conditions and reading back what happened.

Everything the agent learns comes through here. Each run is a fresh
subprocess, so process-level state (hash seed, imports, module globals) starts
clean unless an experiment deliberately varies it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# `-o addopts=` neutralizes the repo's own default flags (coverage, -x,
# distributed runners) so an experiment measures the test, not the config.
# `-p no:randomly` stops a shuffling plugin from confounding order experiments.
# The reporting plugin gives back exact node ids; see _report_plugin.
PYTEST_BASE = [
    "-p", "no:cacheprovider",
    "-p", "no:randomly",
    "-p", "heisenbug._report_plugin",
    "-o", "addopts=",
    "-q",
    "--tb=line",
]

REPORT_ENV_VAR = "HEISENBUG_REPORT"

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
MISSING = "not-run"


class BudgetExhausted(RuntimeError):
    """Raised when an investigation has used up its allowance of test runs."""


@dataclass
class RunBudget:
    """A hard cap on how many pytest processes one investigation may spawn."""

    limit: int = 60
    used: int = 0

    def spend(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise BudgetExhausted(
                f"this investigation has used its {self.limit}-run budget; "
                "conclude from what you already observed"
            )
        self.used += n

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


@dataclass
class RunResult:
    """The outcome of one pytest process."""

    exit_code: int
    outcomes: dict[str, str] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0
    timed_out: bool = False
    collection_error: str = ""
    stdout_tail: str = ""

    def outcome_of(self, node_id: str) -> str:
        if node_id in self.outcomes:
            return self.outcomes[node_id]
        # JUnit XML cannot always reproduce a node id exactly (parametrized
        # ids, nested classes), so fall back to a unique suffix match.
        suffix = node_id.split("::")[-1]
        hits = [k for k in self.outcomes if k.split("::")[-1] == suffix]
        if len(hits) == 1:
            return self.outcomes[hits[0]]
        return MISSING

    def message_of(self, node_id: str) -> str:
        if node_id in self.messages:
            return self.messages[node_id]
        suffix = node_id.split("::")[-1]
        hits = [k for k in self.messages if k.split("::")[-1] == suffix]
        return self.messages[hits[0]] if len(hits) == 1 else ""

    def failed(self, node_id: str) -> bool:
        return self.outcome_of(node_id) in (FAILED, ERROR)


def _parse_report(report_path: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Read the JSON-lines report the plugin wrote."""
    outcomes: dict[str, str] = {}
    messages: dict[str, str] = {}
    collect_errors: list[str] = []

    if not report_path.is_file():
        return outcomes, messages, collect_errors

    for line in report_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        node_id = record.get("nodeid", "")
        outcome = record.get("outcome", "")
        when = record.get("when", "")
        message = (record.get("message") or "").replace("\n", " ").strip()

        if record.get("kind") == "collect_error":
            collect_errors.append(f"{node_id}: {message[:300]}")
            continue
        if not node_id:
            continue

        if outcome == "failed":
            # A failure during setup or teardown is an error, not a test
            # failure — the distinction points at fixtures rather than asserts.
            resolved = FAILED if when == "call" else ERROR
        elif outcome == "skipped":
            resolved = SKIPPED
        else:
            resolved = PASSED

        # A later failure (e.g. in teardown) outranks an earlier pass.
        if outcomes.get(node_id) in (FAILED, ERROR):
            continue
        outcomes[node_id] = resolved
        if message:
            messages[node_id] = message[:400]

    return outcomes, messages, collect_errors


class PytestRunner:
    """Runs pytest in a subprocess and parses structured results."""

    def __init__(
        self,
        repo_root: Path,
        budget: RunBudget | None = None,
        default_timeout: int = 300,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.budget = budget or RunBudget()
        self.default_timeout = default_timeout

    def collect(self, path_filter: str = "") -> tuple[list[str], str]:
        """Collect test node ids without running anything."""
        args = [*PYTEST_BASE, "--collect-only"]
        if path_filter:
            args.append(path_filter)
        proc = self._spawn(args, env={}, timeout=120)
        node_ids = [
            line.strip()
            for line in proc.stdout.splitlines()
            if "::" in line and not line.startswith(("=", "-", "<", "ERROR", "E "))
        ]
        return node_ids, proc.stdout[-2000:]

    def run(
        self,
        node_ids: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        charge_budget: bool = True,
    ) -> RunResult:
        """Run the given tests (or the whole suite) once, in a fresh process."""
        if charge_budget:
            self.budget.spend()

        with tempfile.TemporaryDirectory(prefix="heisenbug-") as tmp:
            report_path = Path(tmp) / "report.jsonl"
            args = [*PYTEST_BASE, *(node_ids or [])]
            run_env = {**(env or {}), REPORT_ENV_VAR: str(report_path)}

            started = time.monotonic()
            proc = self._spawn(args, env=run_env, timeout=timeout or self.default_timeout)
            duration = time.monotonic() - started

            outcomes, messages, collect_errors = _parse_report(report_path)

        collection_error = "; ".join(collect_errors[:3])
        if proc.returncode == 5:
            collection_error = "pytest collected no tests (exit code 5)"
        elif proc.returncode == 4:
            collection_error = "pytest usage error (exit code 4) — a node id is probably malformed"
        elif proc.returncode == 3:
            collection_error = "pytest internal error (exit code 3)"
        elif proc.returncode == 2 and not outcomes:
            collection_error = "pytest was interrupted before running tests (exit code 2)"

        return RunResult(
            exit_code=proc.returncode,
            outcomes=outcomes,
            messages=messages,
            duration_s=duration,
            timed_out=getattr(proc, "timed_out", False),
            collection_error=collection_error,
            stdout_tail=(proc.stdout or "")[-1500:],
        )

    def run_repeated(
        self,
        node_ids: list[str] | None = None,
        *,
        repeat: int = 1,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> list[RunResult]:
        repeat = max(1, min(int(repeat), self.budget.remaining))
        return [self.run(node_ids, env=env, timeout=timeout) for _ in range(repeat)]

    def _spawn(self, args: list[str], env: dict[str, str], timeout: int):
        merged = {**os.environ, **{k: str(v) for k, v in env.items()}}
        merged.pop("PYTEST_CURRENT_TEST", None)
        cmd = [sys.executable, "-m", "pytest", *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.repo_root,
                env=merged,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            proc.timed_out = False  # type: ignore[attr-defined]
            return proc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            result = subprocess.CompletedProcess(cmd, returncode=-1, stdout=stdout, stderr="")
            result.timed_out = True  # type: ignore[attr-defined]
            return result


def bisect_polluter(
    runner: PytestRunner,
    target: str,
    candidates: list[str],
    env: dict[str, str] | None = None,
) -> tuple[list[str], int, str]:
    """Binary-search the preceding tests for the one that breaks `target`.

    Returns the minimal prefix that still reproduces the failure, how many
    pytest runs it cost, and a note on what happened.
    """
    runs = 0

    def fails_with(prefix: list[str]) -> bool:
        nonlocal runs
        runs += 1
        result = runner.run([*prefix, target], env=env)
        return result.failed(target)

    candidates = [c for c in candidates if c != target]
    if not candidates:
        return [], 0, "no candidate tests to bisect"

    if not fails_with(candidates):
        return [], runs, "target passed with the full candidate prefix — not reproducible this way"

    prefix = candidates
    while len(prefix) > 1:
        half = len(prefix) // 2
        first, second = prefix[:half], prefix[half:]
        if fails_with(second):
            prefix = second
            continue
        if fails_with(first):
            prefix = first
            continue
        return (
            prefix,
            runs,
            "narrowed to a set that only reproduces together — the tests interact as a group, "
            "not through a single polluter",
        )

    return prefix, runs, "isolated a single polluting test"
