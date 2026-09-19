"""The investigation loop: hypothesis, experiment, verdict."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import anthropic

from .report import FlakeReport
from .runner import PytestRunner, RunBudget
from .scan import ScanResult, scan as run_scan
from .tools import HuntContext, build_tools

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_ITERATIONS = 50

SYSTEM_PROMPT = """\
You are Heisenbug, a flaky-test investigator. A test that fails sometimes is \
not a test to retry — it is a test whose cause you have not found yet. Your job \
is to find the cause by experiment and name the fix.

You have a run budget, measured in pytest processes. Spend it on experiments \
that would distinguish between hypotheses, not on re-running things you have \
already observed. Prefer the experiment that splits the possibilities in half.

The decision that matters most comes first: does the test fail on its own?

- Run it in isolation. If it fails alone, the cause is inside the test or its \
fixtures — randomness without a seed, the wall clock, hash and set iteration \
order, a real network or filesystem dependency. Distinguish between those by \
holding one variable still at a time: pin PYTHONHASHSEED across several values \
and see whether failures track the seed; pin TZ; read the source and look for \
the nondeterministic call.
- If it always passes alone but fails in the suite, the cause is another test. \
Bisect for the polluting test, then read that test's source to find the state \
it leaves behind — a module-level global, a patched attribute never restored, a \
cached connection, a changed working directory.

Rules of evidence:
- A cause is established by an experiment that would have come out differently \
if you were wrong. State that experiment in your evidence.
- Claim high confidence only when you isolated the cause and reproduced it. If \
the budget ran out or the failure never reproduced, say the cause is \
undetermined and report what you ruled out — that is a real result, and far more \
useful than a confident guess.
- "Fails every time in the suite" and "fails every time on its own" are \
different findings. A test that fails in every suite run but passes alone is \
order-dependent — the single most common "works on my machine" failure — so \
check isolation before calling anything broken. Only a test that also fails \
alone, every time, is genuinely broken; say so and move on without bisecting it.
- Never propose a retry, a rerun plugin, or a sleep as the fix. The fix is the \
change that removes the nondeterminism.

Investigate each suspect, then stop and summarize what you established for each \
one, with the experiment that showed it.
"""

VERDICT_PROMPT = """\
Here is the record of a flaky-test investigation.

<investigation>
{transcript}
</investigation>

<experiment_log>
{experiments}
</experiment_log>

Produce the structured report.

One diagnosis per suspect test that was investigated. Set `cause` only to what \
an experiment showed; when the investigation did not establish a mechanism, use \
`undetermined` with low confidence and say in the evidence what was ruled out. \
Never invent a failure rate or a reproducer command that was not observed — \
quote the numbers from the log. `fix` must be a concrete code change, never a \
retry or a rerun.

The verdict is BROKEN if any test fails deterministically, FLAKY if any test is \
unreliable, CLEAN only if nothing was found.
"""


class HuntError(RuntimeError):
    """Raised when the investigation could not be completed."""


def _text_of(message) -> str:
    return "\n".join(b.text for b in message.content if b.type == "text").strip()


def hunt(
    repo_root: Path,
    *,
    scan_result: ScanResult | None = None,
    repeats: int = 5,
    path_filter: str = "",
    budget: int = 60,
    client: anthropic.Anthropic | None = None,
    model: str = MODEL,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[FlakeReport, int]:
    """Investigate the unstable tests in a repository.

    Returns the report and how many pytest runs the investigation cost.
    """
    client = client or anthropic.Anthropic()
    progress = on_progress or (lambda _line: None)
    repo_root = Path(repo_root).resolve()

    if scan_result is None:
        progress(f"Scanning: running the suite {repeats}x to find unstable tests...")
        scan_result = run_scan(repo_root, repeats=repeats, path_filter=path_filter)

    runner = PytestRunner(repo_root, budget=RunBudget(limit=budget))
    all_tests, _ = runner.collect(path_filter)

    ctx = HuntContext(
        repo_root=repo_root,
        runner=runner,
        scan=scan_result,
        all_tests=all_tests,
    )
    tools = build_tools(ctx)

    suspects = [s.node_id for s in scan_result.suspects]
    broken = [s.node_id for s in scan_result.always_failing]

    if not suspects and not broken:
        return (
            FlakeReport(
                verdict="CLEAN",
                headline=(
                    f"No unstable tests: the suite agreed with itself across "
                    f"{scan_result.repeats} runs."
                ),
                tests_examined=0,
                suite_notes=f"{len(scan_result.stability)} tests observed.",
            ),
            runner.budget.used,
        )

    brief = json.dumps(
        {
            "suspects": [
                {"test": s.node_id, "failure_rate": s.summary, "messages": s.messages[:2]}
                for s in scan_result.suspects
            ],
            "always_failing": broken,
            "suite_size": len(all_tests),
            "run_budget": budget,
        },
        indent=2,
    )

    runner_kwargs = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        tools=tools,
    )

    tool_runner = client.beta.messages.tool_runner(
        **runner_kwargs,
        messages=[
            {
                "role": "user",
                "content": (
                    "A scan of this test suite found these unstable tests:\n\n"
                    f"{brief}\n\n"
                    "Investigate each one and establish why it is unreliable."
                ),
            }
        ],
    )

    transcript: list[str] = []
    iterations = 0
    final_message = None

    for message in tool_runner:
        iterations += 1
        final_message = message

        if message.stop_reason == "refusal":
            raise HuntError(
                "The model declined to continue "
                f"(category: {getattr(message.stop_details, 'category', None)})."
            )

        text = _text_of(message)
        if text:
            transcript.append(text)
            progress(text)

        for block in message.content:
            if block.type == "tool_use":
                args = json.dumps(block.input, sort_keys=True)[:300]
                transcript.append(f"[experiment] {block.name}({args})")
                progress(f"  -> {block.name}({args})")

        if iterations >= MAX_ITERATIONS:
            transcript.append(f"[halted after {MAX_ITERATIONS} iterations]")
            break

    if final_message is not None and final_message.stop_reason == "max_tokens":
        transcript.append("[the final summary was cut off by the token limit]")

    verdict = client.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": VERDICT_PROMPT.format(
                    transcript="\n\n".join(transcript) or "(no output)",
                    experiments="\n".join(ctx.log) or "(no experiments were run)",
                ),
            }
        ],
        output_format=FlakeReport,
    )

    report = verdict.parsed_output
    if report is None:
        raise HuntError("The model did not return a parseable report.")
    return report, runner.budget.used
