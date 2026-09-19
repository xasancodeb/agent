"""The drill loop: let Claude run the restore, then commit to a verdict."""

from __future__ import annotations

import json
from typing import Callable

import anthropic

from .config import DrillConfig
from .report import DrillReport
from .sandbox import Sandbox
from .tools import DrillContext, build_tools

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_ITERATIONS = 60

SYSTEM_PROMPT = """\
You are Lazarus, a backup restore-drill agent. Your job is to find out whether \
the backups you are given would actually survive being needed — not whether a \
backup job reported success.

Treat every backup as guilty until proven restorable. The failures that matter \
are the quiet ones: an archive that unpacks with errors, a database that opens \
but fails integrity_check, a dump that is three days stale because the cron job \
died, a table that restored with a tenth of its rows, a manifest that no longer \
matches what is on disk.

For each target, work through this:

1. list_artifacts — is there anything to restore at all, and how old is it?
2. restore_artifact — restore the newest artifact into the sandbox. A restore \
that fails is a critical finding and the target is FAIL; say so and move on to \
the next target rather than retrying.
3. Inspect what came back. For SQLite: inspect_sqlite for integrity and row \
counts, then run_readonly_query for anything the inventory cannot answer. For \
archives and file trees: list_restored_files, and verify_manifest when a \
manifest is configured.
4. Check the target's declared expectations and freshness budget. A restore that \
works but violates them is DEGRADED, not PASS.

When something looks wrong, investigate before concluding. An empty table might \
be genuinely empty; query it. A low row count might be a truncated dump; compare \
it against the other tables and against the artifact size. Your value is in \
telling an operator *which* of those it is.

Be economical with tool calls — you are checking backups, not exploring. When \
every target has been drilled, stop and summarize what you found, naming each \
target, its outcome, and the evidence. Do not restate the whole transcript.
"""

VERDICT_PROMPT = """\
Here is the complete record of a backup restore drill.

<drill_transcript>
{transcript}
</drill_transcript>

<tool_observations>
{observations}
</tool_observations>

Produce the structured drill report.

Rules for the verdict:
- FAIL if any target's artifact is missing, could not be restored, failed an \
integrity check, or is so incomplete that a real restore would not serve.
- DEGRADED if every target restored but at least one is stale, violates a \
declared expectation, has manifest mismatches, or looks materially incomplete.
- PASS only when every target restored cleanly and met its expectations.

The overall verdict is the worst of the per-target verdicts. Every target that \
was drilled gets a TargetResult. Every problem gets a Finding whose evidence \
quotes the actual observation it came from — never invent numbers. Write the \
headline for someone who is woken up by it.
"""


class DrillError(RuntimeError):
    """Raised when the drill could not be completed."""


def _text_of(message) -> str:
    return "\n".join(b.text for b in message.content if b.type == "text").strip()


def run_drill(
    config: DrillConfig,
    *,
    client: anthropic.Anthropic | None = None,
    model: str = MODEL,
    keep_sandbox: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> DrillReport:
    """Drill every target in the config and return a structured report."""
    client = client or anthropic.Anthropic()
    progress = on_progress or (lambda _line: None)

    with Sandbox(keep=keep_sandbox) as sandbox:
        ctx = DrillContext(config=config, sandbox=sandbox)
        tools = build_tools(ctx)

        target_brief = json.dumps(
            [
                {
                    "name": t.name,
                    "kind": t.kind,
                    "artifact_glob": t.artifact_glob,
                    "freshness_hours": t.freshness_hours,
                    "manifest": t.manifest,
                    "expectations": t.expectations.describe(),
                }
                for t in config.targets
            ],
            indent=2,
        )

        runner = client.beta.messages.tool_runner(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=tools,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Drill these backup targets and report what you find:\n\n"
                        f"{target_brief}\n\n"
                        "Restore each one and verify it would serve a real recovery."
                    ),
                }
            ],
        )

        transcript: list[str] = []
        iterations = 0
        final_message = None

        for message in runner:
            iterations += 1
            final_message = message

            if message.stop_reason == "refusal":
                raise DrillError(
                    "The model declined to continue the drill "
                    f"(category: {getattr(message.stop_details, 'category', None)})."
                )

            text = _text_of(message)
            if text:
                transcript.append(text)
                progress(text)

            for block in message.content:
                if block.type == "tool_use":
                    args = json.dumps(block.input, sort_keys=True)
                    transcript.append(f"[tool] {block.name}({args})")
                    progress(f"  -> {block.name}({args})")

            if iterations >= MAX_ITERATIONS:
                transcript.append(
                    f"[drill halted after {MAX_ITERATIONS} iterations without finishing]"
                )
                break

        if final_message is not None and final_message.stop_reason == "max_tokens":
            transcript.append("[the drill's final summary was cut off by the token limit]")

        observations = "\n".join(ctx.transcript) or "(no tool observations recorded)"

        verdict = client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": VERDICT_PROMPT.format(
                        transcript="\n\n".join(transcript) or "(the drill produced no output)",
                        observations=observations,
                    ),
                }
            ],
            output_format=DrillReport,
        )

        report = verdict.parsed_output
        if report is None:
            raise DrillError("The model did not return a parseable drill report.")
        return report
