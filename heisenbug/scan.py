"""The deterministic half: find which tests are unstable, before asking why.

This runs with no model and no API key. It answers "which tests do not agree
with themselves" — the agent's job starts after that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .runner import ERROR, FAILED, MISSING, PASSED, PytestRunner, RunBudget, SKIPPED


@dataclass
class Stability:
    node_id: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    missing: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def runs(self) -> int:
        return self.passed + self.failed + self.skipped + self.missing

    @property
    def status(self) -> str:
        if self.failed and self.passed:
            return "intermittent"
        if self.failed and not self.passed:
            return "always-failing"
        if self.missing and self.passed:
            return "sometimes-not-run"
        return "stable"

    @property
    def summary(self) -> str:
        return f"{self.failed}/{self.runs} runs failed"


@dataclass
class ScanResult:
    repeats: int
    stability: dict[str, Stability]
    run_errors: list[str] = field(default_factory=list)

    def by_status(self, status: str) -> list[Stability]:
        return sorted(
            (s for s in self.stability.values() if s.status == status),
            key=lambda s: s.node_id,
        )

    @property
    def intermittent(self) -> list[Stability]:
        return self.by_status("intermittent")

    @property
    def always_failing(self) -> list[Stability]:
        return self.by_status("always-failing")

    @property
    def suspects(self) -> list[Stability]:
        return self.intermittent + self.by_status("sometimes-not-run")


def scan(
    repo_root: Path,
    *,
    repeats: int = 5,
    path_filter: str = "",
    runner: PytestRunner | None = None,
) -> ScanResult:
    """Run the suite `repeats` times and tally how each test behaved."""
    runner = runner or PytestRunner(repo_root, budget=RunBudget(limit=repeats + 5))
    node_ids = [path_filter] if path_filter else None

    stability: dict[str, Stability] = {}
    errors: list[str] = []

    for _ in range(repeats):
        result = runner.run(node_ids)
        if result.collection_error:
            errors.append(result.collection_error)
        if result.timed_out:
            errors.append("a run timed out before finishing")

        seen = set(stability)
        for node_id, outcome in result.outcomes.items():
            entry = stability.setdefault(node_id, Stability(node_id=node_id))
            if outcome == PASSED:
                entry.passed += 1
            elif outcome in (FAILED, ERROR):
                entry.failed += 1
                message = result.messages.get(node_id, "")
                if message and message not in entry.messages:
                    entry.messages.append(message)
            elif outcome == SKIPPED:
                entry.skipped += 1

        # A test that ran before but not this time is itself a signal — an
        # earlier crash or a collection that changed under us.
        for node_id in seen - set(result.outcomes):
            stability[node_id].missing += 1

    return ScanResult(repeats=repeats, stability=stability, run_errors=errors)


def render_scan(result: ScanResult) -> str:
    lines = ["", f"  Ran the suite {result.repeats}x — {len(result.stability)} tests seen", ""]

    for error in dict.fromkeys(result.run_errors):
        lines.append(f"  ! {error}")
    if result.run_errors:
        lines.append("")

    if result.intermittent:
        lines.append("  Intermittent (the real flakes):")
        for entry in result.intermittent:
            lines.append(f"    {entry.node_id} — {entry.summary}")
            for message in entry.messages[:2]:
                lines.append(f"        {message[:160]}")
    else:
        lines.append("  No intermittent tests observed.")

    missing = result.by_status("sometimes-not-run")
    if missing:
        lines.append("")
        lines.append("  Sometimes not run at all (collection or crash):")
        for entry in missing:
            lines.append(f"    {entry.node_id} — missing from {entry.missing}/{entry.runs} runs")

    if result.always_failing:
        lines.append("")
        lines.append("  Always failing (broken, not flaky):")
        for entry in result.always_failing:
            lines.append(f"    {entry.node_id}")

    lines.append("")
    return "\n".join(lines)
