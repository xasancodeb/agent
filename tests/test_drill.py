"""Tests for the parts of the drill that must work without calling the API."""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from lazarus.config import ConfigError, DrillConfig, Target
from lazarus.demo import build as build_demo
from lazarus.report import DrillReport, render_text
from lazarus.sandbox import RestoreError, Sandbox, find_artifacts, restore
from lazarus.tools import DrillContext, build_tools


def _tools_by_name(ctx: DrillContext) -> dict:
    return {t.name: t for t in build_tools(ctx)}


def _call(tool, **kwargs) -> str:
    """Invoke a @beta_tool-decorated tool the way the runner would."""
    return tool.call(kwargs)


@pytest.fixture
def demo(tmp_path: Path) -> DrillConfig:
    config_path = build_demo(tmp_path)
    return DrillConfig.load(config_path)


# --- config -----------------------------------------------------------------


def test_config_rejects_unknown_kind(tmp_path: Path):
    path = tmp_path / "drill.yaml"
    path.write_text("targets:\n  - name: x\n    kind: postgres\n    artifact_glob: '*.sql'\n")
    with pytest.raises(ConfigError, match="kind"):
        DrillConfig.load(path)


def test_config_rejects_duplicate_targets(tmp_path: Path):
    path = tmp_path / "drill.yaml"
    path.write_text(
        "targets:\n"
        "  - {name: a, kind: files, artifact_glob: 'x/*'}\n"
        "  - {name: a, kind: files, artifact_glob: 'y/*'}\n"
    )
    with pytest.raises(ConfigError, match="duplicate"):
        DrillConfig.load(path)


def test_config_rejects_table_expectations_on_non_sqlite(tmp_path: Path):
    path = tmp_path / "drill.yaml"
    path.write_text(
        "targets:\n"
        "  - name: a\n    kind: archive\n    artifact_glob: 'x/*.tar.gz'\n"
        "    expectations: {required_tables: [orders]}\n"
    )
    with pytest.raises(ConfigError, match="sqlite"):
        DrillConfig.load(path)


def test_relative_globs_resolve_against_config_dir(demo: DrillConfig):
    for target in demo.targets:
        assert Path(target.artifact_glob).is_absolute()


# --- restore ----------------------------------------------------------------


def test_restore_refuses_empty_artifact(tmp_path: Path):
    artifact = tmp_path / "backup.db"
    artifact.write_bytes(b"")
    target = Target(name="t", kind="sqlite", artifact_glob=str(artifact))
    found = find_artifacts(target)
    with Sandbox() as sandbox:
        with pytest.raises(RestoreError, match="empty"):
            restore(target, found[0], sandbox)


def test_restore_refuses_path_traversal_in_archive(tmp_path: Path):
    payload = tmp_path / "evil.txt"
    payload.write_text("pwned")
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="../../escaped.txt")

    target = Target(name="t", kind="archive", artifact_glob=str(archive))
    found = find_artifacts(target)
    with Sandbox() as sandbox:
        with pytest.raises(RestoreError, match="escapes the sandbox"):
            restore(target, found[0], sandbox)


def test_sandbox_is_removed_on_exit():
    with Sandbox() as sandbox:
        root = sandbox.root
        assert root.exists()
    assert not root.exists()


def test_restore_never_touches_the_source(demo: DrillConfig):
    target = demo.get("orders-healthy")
    artifact = find_artifacts(target)[0]
    before = artifact.path.read_bytes()
    with Sandbox() as sandbox:
        restored, _ = restore(target, artifact, sandbox)
        with sqlite3.connect(restored) as conn:
            conn.execute("SELECT COUNT(*) FROM orders").fetchone()
    assert artifact.path.read_bytes() == before


# --- tools ------------------------------------------------------------------


def test_healthy_backup_passes_inspection(demo: DrillConfig):
    with Sandbox() as sandbox:
        ctx = DrillContext(config=demo, sandbox=sandbox)
        tools = _tools_by_name(ctx)
        _call(tools["restore_artifact"], target="orders-healthy")
        report = _call(tools["inspect_sqlite"], target="orders-healthy")

    assert "integrity_check: ok" in report
    assert "orders: 2,400 rows" in report
    assert "customers: 300 rows" in report


def test_truncated_backup_is_caught(demo: DrillConfig):
    with Sandbox() as sandbox:
        ctx = DrillContext(config=demo, sandbox=sandbox)
        tools = _tools_by_name(ctx)
        _call(tools["restore_artifact"], target="orders-broken")
        report = _call(tools["inspect_sqlite"], target="orders-broken")

    # The file opens and looks like a database; only a real check exposes it.
    assert "integrity_check: ok" not in report


def test_manifest_verification_reports_the_missing_file(demo: DrillConfig):
    with Sandbox() as sandbox:
        ctx = DrillContext(config=demo, sandbox=sandbox)
        tools = _tools_by_name(ctx)
        _call(tools["restore_artifact"], target="uploads")
        report = _call(tools["verify_manifest"], target="uploads")

    assert "6 verified" in report
    assert "1 missing" in report
    assert "upload-07.dat" in report


def test_query_tool_rejects_writes(demo: DrillConfig):
    with Sandbox() as sandbox:
        ctx = DrillContext(config=demo, sandbox=sandbox)
        tools = _tools_by_name(ctx)
        _call(tools["restore_artifact"], target="orders-healthy")

        assert "REJECTED" in _call(
            tools["run_readonly_query"], target="orders-healthy", sql="DELETE FROM orders"
        )
        assert "REJECTED" in _call(
            tools["run_readonly_query"],
            target="orders-healthy",
            sql="SELECT 1; DROP TABLE orders",
        )
        rows = _call(
            tools["run_readonly_query"],
            target="orders-healthy",
            sql="SELECT MAX(placed_at) AS newest FROM orders",
        )
        assert "newest" in rows


def test_inspecting_before_restoring_is_an_error(demo: DrillConfig):
    with Sandbox() as sandbox:
        ctx = DrillContext(config=demo, sandbox=sandbox)
        tools = _tools_by_name(ctx)
        assert "restore_artifact first" in _call(tools["inspect_sqlite"], target="orders-healthy")


def test_missing_artifacts_are_reported_not_raised(tmp_path: Path):
    config_path = tmp_path / "drill.yaml"
    config_path.write_text(
        "targets:\n  - {name: ghost, kind: sqlite, artifact_glob: 'nowhere/*.db'}\n"
    )
    config = DrillConfig.load(config_path)
    with Sandbox() as sandbox:
        ctx = DrillContext(config=config, sandbox=sandbox)
        tools = _tools_by_name(ctx)
        assert "No artifacts matched" in _call(tools["list_artifacts"], target="ghost")
        assert "FAILED" in _call(tools["restore_artifact"], target="ghost")


# --- report -----------------------------------------------------------------


def test_exit_code_tracks_verdict():
    def report(verdict: str) -> DrillReport:
        return DrillReport(verdict=verdict, headline="h", targets=[])

    assert report("PASS").exit_code == 0
    assert report("DEGRADED").exit_code == 1
    assert report("FAIL").exit_code == 2


def test_render_text_includes_findings():
    report = DrillReport(
        verdict="FAIL",
        headline="orders-broken would not restore.",
        targets=[
            {
                "target": "orders-broken",
                "restored": True,
                "artifact": "/backups/orders-20260919.db",
                "verdict": "FAIL",
                "notes": "Database opens but fails integrity_check.",
            }
        ],
        findings=[
            {
                "target": "orders-broken",
                "severity": "critical",
                "summary": "Backup is truncated.",
                "evidence": "integrity_check reported page errors",
                "remediation": "Re-run the dump and verify the exit status.",
            }
        ],
        next_steps=["Re-run last night's dump."],
    )
    text = render_text(report)
    assert "FAIL" in text
    assert "Backup is truncated." in text
    assert "Re-run last night's dump." in text
