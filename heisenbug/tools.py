"""The experiments the agent can run.

The agent's job is to form a hypothesis about why a test is unreliable and
then design an experiment that would distinguish it from the alternatives.
These tools are those experiments. The expensive, well-defined search
(bisecting the suite for a polluting test) is done deterministically here;
choosing when to run it is the agent's call.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import beta_tool

from .runner import (
    BudgetExhausted,
    PytestRunner,
    RunBudget,
    bisect_polluter,
)
from .scan import ScanResult

MAX_RESULT_CHARS = 6000
MAX_BISECT_CANDIDATES = 400


@dataclass
class HuntContext:
    """Per-investigation state shared by every tool."""

    repo_root: Path
    runner: PytestRunner
    scan: ScanResult | None = None
    all_tests: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.log.append(line)

    @property
    def budget(self) -> RunBudget:
        return self.runner.budget


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n[...truncated]"


def _parse_env(env: str) -> dict[str, str]:
    """Parse 'TZ=UTC PYTHONHASHSEED=0' into a dict."""
    overrides: dict[str, str] = {}
    for token in env.split():
        if "=" in token:
            key, _, value = token.partition("=")
            overrides[key.strip()] = value.strip()
    return overrides


def _outcome_line(result, node_id: str) -> str:
    outcome = result.outcome_of(node_id)
    message = result.message_of(node_id)
    return f"{outcome}" + (f" — {message[:200]}" if message else "")


def _tests_before(ctx: HuntContext, target: str) -> list[str]:
    """Every test that runs before `target` in the suite's natural order."""
    if target not in ctx.all_tests:
        return list(ctx.all_tests)
    return ctx.all_tests[: ctx.all_tests.index(target)]


def build_tools(ctx: HuntContext) -> list:
    """Build the experiment tools bound to this investigation."""

    @beta_tool
    def show_scan() -> str:
        """Show the baseline scan: which tests were unstable and how often.

        Start here. The scan already ran the whole suite several times before
        you were called, so this costs nothing.
        """
        if ctx.scan is None:
            return "No baseline scan is available for this investigation."

        lines = [
            f"Baseline: the suite ran {ctx.scan.repeats}x, {len(ctx.scan.stability)} tests seen.",
            f"Run budget remaining: {ctx.budget.remaining} pytest runs.",
        ]
        for error in dict.fromkeys(ctx.scan.run_errors):
            lines.append(f"RUN ERROR: {error}")

        intermittent = ctx.scan.intermittent
        lines.append(f"\nIntermittent ({len(intermittent)}):")
        for entry in intermittent:
            lines.append(f"  {entry.node_id} — {entry.summary}")
            for message in entry.messages[:2]:
                lines.append(f"      {message[:200]}")

        for entry in ctx.scan.by_status("sometimes-not-run"):
            lines.append(f"  {entry.node_id} — did not run in {entry.missing}/{entry.runs} runs")

        always_failing = ctx.scan.always_failing
        if always_failing:
            lines.append(f"\nAlways failing ({len(always_failing)}) — broken, not flaky:")
            for entry in always_failing:
                lines.append(f"  {entry.node_id}")
                for message in entry.messages[:1]:
                    lines.append(f"      {message[:200]}")

        lines.append(f"\nSuite order ({len(ctx.all_tests)} tests): natural collection order.")
        return _truncate("\n".join(lines))

    @beta_tool
    def run_in_isolation(test: str, repeat: int = 5) -> str:
        """Run one test by itself, in a fresh process, several times.

        This is the first experiment for any suspect. A test that fails in the
        suite but always passes alone is not broken by itself — something else
        in the suite is breaking it, and you should bisect for the culprit. A
        test that fails intermittently even alone carries its own cause:
        randomness, the clock, hash ordering, or an external dependency.

        Args:
            test: The pytest node id to run.
            repeat: How many separate processes to run it in.
        """
        try:
            results = ctx.runner.run_repeated([test], repeat=repeat)
        except BudgetExhausted as exc:
            return f"BUDGET: {exc}"

        if not results:
            return "No runs were possible — the budget is exhausted."

        failures = [r for r in results if r.failed(test)]
        missing = [r for r in results if r.outcome_of(test) == "not-run"]
        lines = [f"Ran {test} alone {len(results)}x: {len(failures)} failed."]
        if missing:
            lines.append(f"{len(missing)} run(s) did not execute it at all — check the node id.")
        for result in failures[:3]:
            lines.append(f"  failure: {result.message_of(test)[:250]}")
        if not failures and not missing:
            lines.append(
                "Passed every time in isolation — the cause is outside this test. "
                "Bisect the suite for what precedes it."
            )
        ctx.note(f"[isolation] {test}: {len(failures)}/{len(results)} failed alone")
        return _truncate("\n".join(lines))

    @beta_tool
    def run_with_env(test: str, env: str, repeat: int = 3) -> str:
        """Run a test with specific environment variables, several times.

        Use this to hold a suspected source of nondeterminism still. Pinning
        PYTHONHASHSEED to a fixed value stops set and dict iteration order from
        varying between processes; if a test fails on some seeds and passes on
        others, that is hash ordering, not chance. TZ exposes tests that assume
        a local timezone.

        Args:
            test: The pytest node id to run.
            env: Space-separated assignments, e.g. 'PYTHONHASHSEED=0 TZ=UTC'.
            repeat: How many separate processes to run it in.
        """
        overrides = _parse_env(env)
        if not overrides:
            return "ERROR: no assignments parsed. Use the form 'NAME=value NAME2=value2'."

        try:
            results = ctx.runner.run_repeated([test], repeat=repeat, env=overrides)
        except BudgetExhausted as exc:
            return f"BUDGET: {exc}"
        if not results:
            return "No runs were possible — the budget is exhausted."

        failures = sum(1 for r in results if r.failed(test))
        lines = [f"Ran {test} {len(results)}x with {env}: {failures} failed."]
        for result in results:
            if result.failed(test):
                lines.append(f"  failure: {result.message_of(test)[:250]}")
                break
        ctx.note(f"[env {env}] {test}: {failures}/{len(results)} failed")
        return _truncate("\n".join(lines))

    @beta_tool
    def run_after(test: str, preceding_tests: list[str]) -> str:
        """Run specific tests first, then the target, in one shared process.

        This is how you confirm a suspicion about a particular polluter without
        paying for a full bisect: name the test you think leaves state behind
        and see whether the target fails after it.

        Args:
            test: The pytest node id to run last.
            preceding_tests: Node ids to run first, in order, in the same process.
        """
        if not preceding_tests:
            return "ERROR: name at least one preceding test."
        try:
            result = ctx.runner.run([*preceding_tests, test])
        except BudgetExhausted as exc:
            return f"BUDGET: {exc}"

        if result.collection_error:
            return f"RUN ERROR: {result.collection_error}"

        outcome = _outcome_line(result, test)
        preceding = ", ".join(preceding_tests[:5]) + (
            f" (+{len(preceding_tests) - 5} more)" if len(preceding_tests) > 5 else ""
        )
        ctx.note(f"[after] {test} after {len(preceding_tests)} test(s): {result.outcome_of(test)}")
        return _truncate(
            f"Ran {preceding} then {test}.\nTarget outcome: {outcome}"
        )

    @beta_tool
    def find_polluting_test(test: str) -> str:
        """Binary-search the tests that run before this one for the culprit.

        Use this when a test passes alone but fails in the suite. It narrows
        the preceding tests by halves until a minimal set still reproduces the
        failure, which usually names a single polluting test. Costs roughly
        log2(n) runs, so use it once you have ruled out isolation.

        Args:
            test: The pytest node id that fails only when run with others.
        """
        candidates = _tests_before(ctx, test)
        if not candidates:
            return (
                f"{test} runs first in the suite — nothing precedes it, so test order "
                "cannot be the cause."
            )
        if len(candidates) > MAX_BISECT_CANDIDATES:
            candidates = candidates[-MAX_BISECT_CANDIDATES:]

        try:
            prefix, runs, note = bisect_polluter(ctx.runner, test, candidates)
        except BudgetExhausted as exc:
            return f"BUDGET: {exc}"

        if not prefix:
            ctx.note(f"[bisect] {test}: not reproducible ({note})")
            return f"Bisect used {runs} runs. {note.capitalize()}."

        lines = [f"Bisect used {runs} runs. {note.capitalize()}."]
        lines.append(f"Minimal reproducing prefix ({len(prefix)} test(s)):")
        for node_id in prefix[:10]:
            lines.append(f"  {node_id}")
        lines.append(f"\nReproduce with:\n  pytest {' '.join(prefix[:10])} {test}")
        ctx.note(f"[bisect] {test}: culprit prefix = {prefix[:3]}")
        return _truncate("\n".join(lines))

    @beta_tool
    def read_test_source(test: str) -> str:
        """Read a test's source, plus the module-level state around it.

        Module-level mutable values are the usual mechanism behind
        order-dependent failures, so they are reported separately here. Read
        the culprit's source as well as the victim's.

        Args:
            test: The pytest node id whose source to read.
        """
        file_part = test.split("::")[0]
        path = (ctx.repo_root / file_part).resolve()
        if not path.is_file():
            return f"ERROR: {file_part} is not a file in the repository."
        try:
            path.relative_to(ctx.repo_root)
        except ValueError:
            return "ERROR: refusing to read outside the repository."

        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"ERROR: cannot parse {file_part}: {exc}"

        func_name = test.split("::")[-1].split("[")[0]
        lines = [f"# {file_part}"]

        module_state = []
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                module_state.append(ast.unparse(node))
        if module_state:
            lines.append("\n## Module-level state (shared by every test in this file):")
            lines += [f"  {line}" for line in module_state[:20]]

        fixtures = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = [ast.unparse(d) for d in node.decorator_list]
                if any("fixture" in d for d in decorators):
                    fixtures.append(f"  {node.name}  [{', '.join(decorators)}]")
        if fixtures:
            lines.append("\n## Fixtures defined here:")
            lines += fixtures[:15]

        target_source = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                target_source = ast.get_source_segment(source, node) or ""
                break
        lines.append(f"\n## {func_name}:")
        lines.append(target_source or f"  (could not locate {func_name} in {file_part})")

        ctx.note(f"[source] read {test}")
        return _truncate("\n".join(lines))

    @beta_tool
    def search_tests(pattern: str, path_glob: str = "**/*.py") -> str:
        """Search the repository for a regex — nondeterminism leaves fingerprints.

        Useful patterns: 'random\\.|uuid4|shuffle' for unseeded randomness,
        'datetime\\.now|time\\.time|today' for clock dependence, 'global |^_[A-Z]'
        for shared mutable state, 'sleep|Thread|asyncio' for races.

        Args:
            pattern: A Python regular expression.
            path_glob: Which files to search, as a glob relative to the repo root.
        """
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex: {exc}"

        hits = []
        for path in sorted(ctx.repo_root.glob(path_glob)):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                content = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    rel = path.relative_to(ctx.repo_root)
                    hits.append(f"  {rel}:{number}: {line.strip()[:160]}")
                    if len(hits) >= 60:
                        break
            if len(hits) >= 60:
                break

        if not hits:
            return f"No matches for {pattern!r} in {path_glob}."
        ctx.note(f"[search] {pattern} -> {len(hits)} hit(s)")
        return _truncate(f"{len(hits)} match(es) for {pattern!r}:\n" + "\n".join(hits))

    return [
        show_scan,
        run_in_isolation,
        run_with_env,
        run_after,
        find_polluting_test,
        read_test_source,
        search_tests,
    ]
