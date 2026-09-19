"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anthropic

from .agent import HuntError, MODEL, hunt
from .report import render_report
from .scan import render_scan, scan


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heisenbug",
        description="Find out why a test is flaky, instead of retrying it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_cmd = sub.add_parser(
        "scan",
        help="Run the suite repeatedly and list unstable tests. No API key needed.",
    )
    scan_cmd.add_argument("path", nargs="?", default=".", help="Repository root (default: .)")
    scan_cmd.add_argument("-n", "--repeats", type=int, default=5, help="How many times to run the suite.")
    scan_cmd.add_argument("-k", "--tests", default="", help="Restrict to a path or node id.")
    scan_cmd.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON.")

    hunt_cmd = sub.add_parser("hunt", help="Scan, then investigate each unstable test.")
    hunt_cmd.add_argument("path", nargs="?", default=".", help="Repository root (default: .)")
    hunt_cmd.add_argument("-n", "--repeats", type=int, default=5, help="Scan repetitions.")
    hunt_cmd.add_argument("-k", "--tests", default="", help="Restrict to a path or node id.")
    hunt_cmd.add_argument(
        "-b", "--budget", type=int, default=60,
        help="Maximum pytest runs the investigation may spend (default: 60).",
    )
    hunt_cmd.add_argument("--model", default=MODEL, help=f"Model to run the investigation (default: {MODEL}).")
    hunt_cmd.add_argument("--report", metavar="PATH", help="Write the JSON report here.")
    hunt_cmd.add_argument("--quiet", action="store_true", help="Only print the final report.")
    hunt_cmd.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON.")

    return parser


def _cmd_scan(args: argparse.Namespace) -> int:
    result = scan(Path(args.path), repeats=args.repeats, path_filter=args.tests)

    if args.as_json:
        print(json.dumps(
            {
                "repeats": result.repeats,
                "intermittent": [
                    {"test": s.node_id, "failed": s.failed, "runs": s.runs, "messages": s.messages}
                    for s in result.intermittent
                ],
                "always_failing": [s.node_id for s in result.always_failing],
                "run_errors": result.run_errors,
            },
            indent=2,
        ))
    else:
        print(render_scan(result))

    if result.always_failing:
        return 2
    return 1 if result.suspects else 0


def _cmd_hunt(args: argparse.Namespace) -> int:
    def progress(line: str) -> None:
        if not args.quiet:
            print(line, file=sys.stderr, flush=True)

    report, runs_used = hunt(
        Path(args.path),
        repeats=args.repeats,
        path_filter=args.tests,
        budget=args.budget,
        model=args.model,
        on_progress=progress,
    )

    if args.report:
        Path(args.report).write_text(report.model_dump_json(indent=2) + "\n")

    if args.as_json:
        print(json.dumps(report.model_dump(), indent=2))
    else:
        print(render_report(report, runs_used=runs_used))

    return report.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return _cmd_scan(args)
        return _cmd_hunt(args)
    except HuntError as exc:
        print(f"investigation error: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 3
    except anthropic.AuthenticationError:
        print(
            "auth error: no usable Anthropic credentials. "
            "Set ANTHROPIC_API_KEY or run `ant auth login`. "
            "(`heisenbug scan` works without credentials.)",
            file=sys.stderr,
        )
        return 3
    except anthropic.APIConnectionError as exc:
        print(f"network error reaching the Anthropic API: {exc}", file=sys.stderr)
        return 3
    except anthropic.APIStatusError as exc:
        print(f"API error {exc.status_code}: {exc.message}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
