"""A pytest plugin that records exact node ids and outcomes as JSON lines.

Loaded into the test subprocess with `-p heisenbug._report_plugin`. Reading
results this way (rather than from JUnit XML) matters because the node id
pytest reports is exactly the one that can be fed back on a command line —
which is what every experiment in an investigation depends on.

Writes to the path in HEISENBUG_REPORT; does nothing if that is unset, so the
plugin is inert in an ordinary test run.
"""

from __future__ import annotations

import json
import os

_ENV_VAR = "HEISENBUG_REPORT"


def _emit(record: dict) -> None:
    path = os.environ.get(_ENV_VAR)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def pytest_runtest_logreport(report) -> None:
    interesting = report.when == "call" or report.outcome in ("failed", "skipped")
    if not interesting:
        return

    message = ""
    if report.longrepr is not None:
        message = str(report.longrepr)

    _emit(
        {
            "kind": "test",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "message": message[-600:],
        }
    )


def pytest_collectreport(report) -> None:
    if report.outcome == "failed":
        _emit(
            {
                "kind": "collect_error",
                "nodeid": report.nodeid,
                "when": "collect",
                "outcome": "failed",
                "message": str(report.longrepr)[-600:] if report.longrepr else "",
            }
        )
