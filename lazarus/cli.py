"""Command line entry point: `lazarus drill <config.yaml>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import anthropic

from .agent import DrillError, MODEL, run_drill
from .config import ConfigError, DrillConfig
from .report import render_text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazarus",
        description="Prove your backups actually restore.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    drill = sub.add_parser("drill", help="Run a restore drill against a config.")
    drill.add_argument("config", help="Path to the drill config YAML.")
    drill.add_argument(
        "--report",
        metavar="PATH",
        help="Write the JSON report here in addition to printing a summary.",
    )
    drill.add_argument("--model", default=MODEL, help=f"Model to drive the drill (default: {MODEL}).")
    drill.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="Leave the restore sandbox on disk for inspection.",
    )
    drill.add_argument("--quiet", action="store_true", help="Only print the final verdict.")
    drill.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the report as JSON instead of text.",
    )

    check = sub.add_parser("check", help="Validate a drill config without calling the API.")
    check.add_argument("config", help="Path to the drill config YAML.")

    return parser


def _cmd_check(args: argparse.Namespace) -> int:
    from .sandbox import find_artifacts

    config = DrillConfig.load(args.config)
    print(f"{config.source}: {len(config.targets)} target(s)")
    problems = 0
    for target in config.targets:
        artifacts = find_artifacts(target)
        if artifacts:
            newest = artifacts[0]
            print(
                f"  ok    {target.name} ({target.kind}): {len(artifacts)} artifact(s), "
                f"newest {newest.age_hours:.1f}h old"
            )
        else:
            problems += 1
            print(f"  EMPTY {target.name} ({target.kind}): nothing matches {target.artifact_glob}")
    return 1 if problems else 0


def _cmd_drill(args: argparse.Namespace) -> int:
    config = DrillConfig.load(args.config)

    def progress(line: str) -> None:
        if not args.quiet:
            print(line, file=sys.stderr, flush=True)

    report = run_drill(
        config,
        model=args.model,
        keep_sandbox=args.keep_sandbox,
        on_progress=progress,
    )

    if args.report:
        Path(args.report).write_text(report.model_dump_json(indent=2) + "\n")

    if args.as_json:
        print(json.dumps(report.model_dump(), indent=2))
    else:
        print(render_text(report))

    return report.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check":
            return _cmd_check(args)
        return _cmd_drill(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 3
    except DrillError as exc:
        print(f"drill error: {exc}", file=sys.stderr)
        return 3
    except anthropic.AuthenticationError:
        print(
            "auth error: no usable Anthropic credentials. "
            "Set ANTHROPIC_API_KEY or run `ant auth login`.",
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
