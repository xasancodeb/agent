"""The drill's output contract: a verdict you can alert on."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["PASS", "DEGRADED", "FAIL"]
Severity = Literal["critical", "warning", "info"]

EXIT_CODES: dict[str, int] = {"PASS": 0, "DEGRADED": 1, "FAIL": 2}


class Finding(BaseModel):
    """One thing that is wrong, or worth knowing, about a backup."""

    target: str = Field(description="Name of the drill target this finding belongs to.")
    severity: Severity = Field(
        description=(
            "critical if the backup would not serve a real restore; "
            "warning if it restores but is degraded, stale, or incomplete; "
            "info for observations that need no action."
        )
    )
    summary: str = Field(description="One sentence naming the problem.")
    evidence: str = Field(
        description="The concrete observation from a tool result that supports this finding."
    )
    remediation: str = Field(
        description="The specific next action an operator should take."
    )


class TargetResult(BaseModel):
    """Per-target outcome of the drill."""

    target: str
    restored: bool = Field(description="Whether the artifact restored at all.")
    artifact: str = Field(description="Path of the artifact that was drilled, or '' if none was found.")
    verdict: Verdict
    notes: str = Field(description="One or two sentences on what the restored data looked like.")


class DrillReport(BaseModel):
    """The whole drill, in a shape a monitoring system can consume."""

    verdict: Verdict = Field(
        description="Worst verdict across all targets: FAIL if any backup would not serve a restore."
    )
    headline: str = Field(
        description="One sentence an on-call engineer can read at 3am and act on."
    )
    targets: list[TargetResult]
    findings: list[Finding] = Field(
        default_factory=list,
        description="Every problem found, most severe first. Empty when everything passed.",
    )
    next_steps: list[str] = Field(
        default_factory=list,
        description="Ordered actions to take. Empty when nothing needs doing.",
    )

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]


def render_text(report: DrillReport) -> str:
    """Human-readable rendering for terminal output."""
    mark = {"PASS": "PASS", "DEGRADED": "DEGRADED", "FAIL": "FAIL"}[report.verdict]
    lines = [
        "",
        f"  RESTORE DRILL: {mark}",
        f"  {report.headline}",
        "",
    ]

    lines.append("  Targets")
    for result in report.targets:
        restored = "restored" if result.restored else "DID NOT RESTORE"
        lines.append(f"    [{result.verdict:<8}] {result.target} — {restored}")
        if result.artifact:
            lines.append(f"               artifact: {result.artifact}")
        if result.notes:
            lines.append(f"               {result.notes}")

    if report.findings:
        lines += ["", "  Findings"]
        order = {"critical": 0, "warning": 1, "info": 2}
        for finding in sorted(report.findings, key=lambda f: order[f.severity]):
            lines.append(f"    ({finding.severity}) {finding.target}: {finding.summary}")
            lines.append(f"        evidence:    {finding.evidence}")
            lines.append(f"        remediation: {finding.remediation}")

    if report.next_steps:
        lines += ["", "  Next steps"]
        for i, step in enumerate(report.next_steps, 1):
            lines.append(f"    {i}. {step}")

    lines.append("")
    return "\n".join(lines)
