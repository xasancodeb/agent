"""The tools the agent drives the drill with.

Each tool is a closure over one DrillContext so the agent can only ever reach
the configured targets and the sandbox for this run. Tool results are plain
text — the model reads them, and they also land in the transcript that the
verdict is derived from.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import beta_tool

from .config import DrillConfig, Target
from .sandbox import (
    Artifact,
    RestoreError,
    Sandbox,
    find_artifacts,
    open_readonly,
    restore,
    sha256_file,
    sqlite_databases,
)

MAX_QUERY_ROWS = 50
MAX_RESULT_CHARS = 8000
READONLY_SQL = re.compile(r"^\s*(select|with|pragma|explain)\b", re.IGNORECASE)


@dataclass
class DrillContext:
    """Per-run state shared by every tool."""

    config: DrillConfig
    sandbox: Sandbox
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    transcript: list[str] = field(default_factory=list)

    def log(self, line: str) -> None:
        self.transcript.append(line)

    def target(self, name: str) -> Target:
        return self.config.get(name)

    def restored(self, name: str) -> Path:
        path = self.sandbox.restored_path(name)
        if path is None:
            raise LookupError(
                f"target {name!r} has not been restored yet — call restore_artifact first"
            )
        return path


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n[...truncated, {len(text) - MAX_RESULT_CHARS} more characters]"


def _resolve_db(ctx: DrillContext, target_name: str, database: str) -> Path:
    """Pick which SQLite file inside a restore to talk to."""
    restored = ctx.restored(target_name)
    candidates = sqlite_databases(restored)
    if not candidates:
        raise LookupError(f"no SQLite database found under the restore of {target_name!r}")
    if not database:
        return candidates[0]
    for candidate in candidates:
        if candidate.name == database or str(candidate) == database:
            return candidate
    names = ", ".join(c.name for c in candidates)
    raise LookupError(f"database {database!r} not found in restore; available: {names}")


def build_tools(ctx: DrillContext) -> list:
    """Build the tool set bound to this drill run."""

    @beta_tool
    def list_artifacts(target: str) -> str:
        """List the backup artifacts available for a target, newest first.

        Use this first for each target to see what exists and how old it is.

        Args:
            target: Name of the drill target, as configured.
        """
        try:
            spec = ctx.target(target)
        except KeyError as exc:
            return f"ERROR: {exc}"

        found = find_artifacts(spec)
        if not found:
            ctx.log(f"[{target}] no artifacts matched {spec.artifact_glob}")
            return (
                f"No artifacts matched {spec.artifact_glob!r}. "
                "Nothing has been backed up here, or the path is wrong."
            )

        ctx.artifacts[target] = found[0]
        lines = [
            f"Target {target!r} (kind={spec.kind}): {len(found)} artifact(s) matching {spec.artifact_glob}",
            f"Expectations: {spec.expectations.describe()}",
        ]
        if spec.freshness_hours is not None:
            lines.append(f"Freshness budget: newest artifact must be under {spec.freshness_hours}h old")
        for i, artifact in enumerate(found[:10]):
            marker = " <- newest" if i == 0 else ""
            lines.append(f"  {artifact.describe()}{marker}")
        if len(found) > 10:
            lines.append(f"  [...{len(found) - 10} older artifacts omitted]")

        newest = found[0]
        if spec.freshness_hours is not None and newest.age_hours > spec.freshness_hours:
            lines.append(
                f"STALE: newest artifact is {newest.age_hours:.1f}h old, "
                f"budget is {spec.freshness_hours}h."
            )
        ctx.log(f"[{target}] newest artifact: {newest.describe()}")
        return _truncate("\n".join(lines))

    @beta_tool
    def restore_artifact(target: str, artifact_path: str = "") -> str:
        """Restore a target's backup artifact into an isolated sandbox directory.

        This is the heart of the drill: an artifact that cannot be restored is a
        backup that does not exist. Always restore before inspecting.

        Args:
            target: Name of the drill target, as configured.
            artifact_path: Specific artifact to restore. Leave empty to use the newest.
        """
        try:
            spec = ctx.target(target)
        except KeyError as exc:
            return f"ERROR: {exc}"

        found = find_artifacts(spec)
        if not found:
            return f"FAILED: no artifacts to restore for {target!r}."

        if artifact_path:
            chosen = next((a for a in found if str(a.path) == artifact_path), None)
            if chosen is None:
                available = ", ".join(str(a.path) for a in found[:5])
                return f"ERROR: {artifact_path!r} is not an artifact of {target!r}. Available: {available}"
        else:
            chosen = found[0]

        ctx.artifacts[target] = chosen
        try:
            restored, summary = restore(spec, chosen, ctx.sandbox)
        except RestoreError as exc:
            ctx.log(f"[{target}] RESTORE FAILED: {exc}")
            return f"RESTORE FAILED for {chosen.path}: {exc}"
        except OSError as exc:
            ctx.log(f"[{target}] RESTORE FAILED: {exc}")
            return f"RESTORE FAILED for {chosen.path}: {type(exc).__name__}: {exc}"

        ctx.log(f"[{target}] restored {chosen.path} -> {restored}")
        result = [f"Restored {chosen.path} ({chosen.age_hours:.1f}h old): {summary}"]
        if spec.kind == "sqlite" or sqlite_databases(restored):
            dbs = sqlite_databases(restored)
            result.append("SQLite databases in restore: " + ", ".join(d.name for d in dbs))
        return _truncate("\n".join(result))

    @beta_tool
    def inspect_sqlite(target: str, database: str = "") -> str:
        """Run integrity checks and take a table/row inventory of a restored database.

        Reports PRAGMA integrity_check, every table, and its row count — the
        fastest way to tell a healthy restore from a truncated or corrupt one.

        Args:
            target: Name of the drill target, as configured.
            database: Filename of the database inside the restore. Leave empty for the first one.
        """
        try:
            db_path = _resolve_db(ctx, target, database)
        except LookupError as exc:
            return f"ERROR: {exc}"

        lines = [f"Database: {db_path.name} ({db_path.stat().st_size:,} bytes)"]
        try:
            conn = open_readonly(db_path)
        except sqlite3.Error as exc:
            ctx.log(f"[{target}] cannot open {db_path.name}: {exc}")
            return f"CORRUPT: cannot open {db_path.name} — {exc}"

        try:
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
                verdict = integrity[0][0] if integrity else "(no result)"
                lines.append(f"integrity_check: {verdict}")
                if verdict != "ok":
                    for row in integrity[1:10]:
                        lines.append(f"  {row[0]}")
            except sqlite3.DatabaseError as exc:
                ctx.log(f"[{target}] integrity_check failed on {db_path.name}: {exc}")
                return f"CORRUPT: {db_path.name} failed integrity_check — {exc}"

            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            lines.append(f"tables ({len(tables)}): {', '.join(tables) if tables else '(none)'}")
            for table in tables:
                try:
                    count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    lines.append(f"  {table}: {count:,} rows")
                except sqlite3.DatabaseError as exc:
                    lines.append(f"  {table}: UNREADABLE — {exc}")
        finally:
            conn.close()

        ctx.log(f"[{target}] inspected {db_path.name}: {len(lines)} observations")
        return _truncate("\n".join(lines))

    @beta_tool
    def run_readonly_query(target: str, sql: str, database: str = "") -> str:
        """Run one read-only SQL query against a restored database.

        Use this to check what the inventory cannot tell you — the freshness of
        the newest record, whether a critical row survived, whether a join still
        resolves. Only SELECT, WITH, PRAGMA and EXPLAIN are permitted.

        Args:
            target: Name of the drill target, as configured.
            sql: A single read-only SQL statement.
            database: Filename of the database inside the restore. Leave empty for the first one.
        """
        if not READONLY_SQL.match(sql):
            return "REJECTED: only SELECT, WITH, PRAGMA and EXPLAIN statements are allowed."
        if ";" in sql.strip().rstrip(";"):
            return "REJECTED: run one statement at a time."

        try:
            db_path = _resolve_db(ctx, target, database)
        except LookupError as exc:
            return f"ERROR: {exc}"

        try:
            conn = open_readonly(db_path)
        except sqlite3.Error as exc:
            return f"ERROR: cannot open {db_path.name} — {exc}"

        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(MAX_QUERY_ROWS)
            headers = [d[0] for d in cursor.description] if cursor.description else []
        except sqlite3.Error as exc:
            ctx.log(f"[{target}] query failed: {sql} -> {exc}")
            return f"QUERY ERROR: {exc}"
        finally:
            conn.close()

        if not rows:
            return "(0 rows)"
        lines = [" | ".join(headers)] if headers else []
        lines += [" | ".join("NULL" if v is None else str(v) for v in row) for row in rows]
        if len(rows) == MAX_QUERY_ROWS:
            lines.append(f"[...truncated at {MAX_QUERY_ROWS} rows]")
        ctx.log(f"[{target}] query: {sql.strip()[:120]} -> {len(rows)} row(s)")
        return _truncate("\n".join(lines))

    @beta_tool
    def verify_manifest(target: str) -> str:
        """Check restored files against the target's sha256 manifest.

        The manifest is the claim; the restore is the reality. This is what
        catches files that were silently dropped or altered in transit.

        Args:
            target: Name of the drill target, as configured.
        """
        try:
            spec = ctx.target(target)
            restored = ctx.restored(target)
        except (KeyError, LookupError) as exc:
            return f"ERROR: {exc}"

        if not spec.manifest:
            return f"No manifest configured for {target!r}; nothing to verify."

        manifest_path = Path(spec.manifest)
        if not manifest_path.is_file():
            ctx.log(f"[{target}] manifest missing: {manifest_path}")
            return f"MISSING: manifest {manifest_path} does not exist."

        expected: dict[str, str] = {}
        for line in manifest_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            digest, name = parts
            expected[name.lstrip("*").strip()] = digest.lower()

        if not expected:
            return f"EMPTY: manifest {manifest_path} lists no files."

        root = restored if restored.is_dir() else restored.parent
        missing, mismatched, ok = [], [], 0
        for name, digest in expected.items():
            candidate = root / name
            if not candidate.is_file():
                # Manifests are often written relative to the archive root.
                matches = list(root.rglob(Path(name).name))
                candidate = matches[0] if matches else candidate
            if not candidate.is_file():
                missing.append(name)
                continue
            if sha256_file(candidate) != digest:
                mismatched.append(name)
            else:
                ok += 1

        lines = [
            f"Manifest {manifest_path.name}: {len(expected)} entries, "
            f"{ok} verified, {len(missing)} missing, {len(mismatched)} checksum mismatch."
        ]
        for name in missing[:10]:
            lines.append(f"  MISSING: {name}")
        for name in mismatched[:10]:
            lines.append(f"  MISMATCH: {name}")
        ctx.log(
            f"[{target}] manifest: {ok} ok, {len(missing)} missing, {len(mismatched)} mismatched"
        )
        return _truncate("\n".join(lines))

    @beta_tool
    def list_restored_files(target: str, max_entries: int = 40) -> str:
        """List the files that came out of a restore, with sizes.

        Use it to sanity-check the shape of an extracted archive or file tree.

        Args:
            target: Name of the drill target, as configured.
            max_entries: How many files to list, at most.
        """
        try:
            restored = ctx.restored(target)
        except LookupError as exc:
            return f"ERROR: {exc}"

        if restored.is_file():
            return f"{restored.name}: {restored.stat().st_size:,} bytes (single file restore)"

        files = sorted(p for p in restored.rglob("*") if p.is_file())
        if not files:
            ctx.log(f"[{target}] restore produced no files")
            return "EMPTY: the restore produced no files at all."

        total = sum(p.stat().st_size for p in files)
        capped = max(1, min(int(max_entries), 200))
        lines = [f"{len(files)} files, {total:,} bytes total, under {restored}"]
        for path in files[:capped]:
            lines.append(f"  {path.relative_to(restored)} — {path.stat().st_size:,} bytes")
        if len(files) > capped:
            lines.append(f"  [...{len(files) - capped} more files]")
        ctx.log(f"[{target}] restore contains {len(files)} files, {total:,} bytes")
        return _truncate("\n".join(lines))

    return [
        list_artifacts,
        restore_artifact,
        inspect_sqlite,
        run_readonly_query,
        verify_manifest,
        list_restored_files,
    ]
