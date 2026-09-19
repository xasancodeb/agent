"""The output contract: a diagnosis per flaky test, not a retry count."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["CLEAN", "FLAKY", "BROKEN"]

Cause = Literal[
    "order_dependent",
    "shared_state",
    "unseeded_randomness",
    "hash_ordering",
    "time_dependent",
    "concurrency",
    "external_dependency",
    "resource_leak",
    "genuinely_broken",
    "undetermined",
]

EXIT_CODES: dict[str, int] = {"CLEAN": 0, "FLAKY": 1, "BROKEN": 2}

CAUSE_LABELS: dict[str, str] = {
    "order_dependent": "depends on test order",
    "shared_state": "shares mutable state with another test",
    "unseeded_randomness": "uses randomness without a fixed seed",
    "hash_ordering": "depends on hash/set iteration order",
    "time_dependent": "depends on the wall clock",
    "concurrency": "races with another thread or process",
    "external_dependency": "depends on something outside the suite",
    "resource_leak": "leaks a file, socket, or temp path",
    "genuinely_broken": "fails deterministically — not flaky",
    "undetermined": "cause not established",
}


class Diagnosis(BaseModel):
    """Why one test is unreliable, and what to do about it."""

    test: str = Field(description="The pytest node id of the unreliable test.")
    cause: Cause = Field(
        description="The mechanism behind the failure, established by experiment — not guessed."
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="high only when an experiment isolated the cause and it reproduces."
    )
    failure_rate: str = Field(
        description="Observed failure frequency, e.g. '3/20 runs' or '1/1 with the full suite'."
    )
    culprit: str = Field(
        default="",
        description=(
            "The other test or source location responsible, when one was identified. "
            "Empty when the test is only at fault by itself."
        ),
    )
    evidence: str = Field(
        description="The experiment that established this: what was run, and what changed."
    )
    reproducer: str = Field(
        description="A command that reproduces the failure, or '' if no reliable one was found."
    )
    fix: str = Field(description="The specific change that would make this test deterministic.")


class FlakeReport(BaseModel):
    """What the investigation concluded."""

    verdict: Verdict = Field(
        description="BROKEN if any test fails deterministically; FLAKY if any test is unreliable; else CLEAN."
    )
    headline: str = Field(description="One sentence naming the most important thing found.")
    tests_examined: int = Field(description="How many suspect tests were investigated.")
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    suite_notes: str = Field(
        default="",
        description="Anything about the suite as a whole worth knowing — shared fixtures, patterns worth fixing once.",
    )

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]


def render_report(report: FlakeReport, runs_used: int | None = None) -> str:
    lines = [
        "",
        f"  FLAKE HUNT: {report.verdict}",
        f"  {report.headline}",
        "",
    ]

    if not report.diagnoses:
        lines.append("  No unreliable tests diagnosed.")
        lines.append("")
        return "\n".join(lines)

    order = {"high": 0, "medium": 1, "low": 2}
    for diagnosis in sorted(report.diagnoses, key=lambda d: order[d.confidence]):
        label = CAUSE_LABELS.get(diagnosis.cause, diagnosis.cause)
        lines.append(f"  {diagnosis.test}")
        lines.append(f"    cause      {label}  ({diagnosis.confidence} confidence)")
        lines.append(f"    frequency  {diagnosis.failure_rate}")
        if diagnosis.culprit:
            lines.append(f"    culprit    {diagnosis.culprit}")
        lines.append(f"    evidence   {diagnosis.evidence}")
        if diagnosis.reproducer:
            lines.append(f"    reproduce  {diagnosis.reproducer}")
        lines.append(f"    fix        {diagnosis.fix}")
        lines.append("")

    if report.suite_notes:
        lines.append(f"  Suite notes: {report.suite_notes}")
        lines.append("")

    if runs_used is not None:
        lines.append(f"  ({runs_used} pytest runs used)")
        lines.append("")

    return "\n".join(lines)
