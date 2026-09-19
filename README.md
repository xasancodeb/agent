# Lazarus — the backup restore-drill agent

> A backup you have never restored is a rumor, not a backup.

Lazarus is an AI agent that does the one crucial thing almost nobody does:
it **proves your backups actually restore**. On every run it takes your most
recent backup artifact, restores it into a disposable sandbox, probes the
restored data, and has Claude diagnose anything suspicious — then issues a
structured **restore-confidence verdict** you can wire into alerting or CI.

Backups fail silently: truncated dumps, archives corrupted in transit,
manifests that drift from reality, restores that "succeed" with half the rows
missing. You discover it on the worst day of your company's life. Lazarus
makes that discovery happen on a quiet Tuesday instead.

## What it does

1. **Discovers** backup artifacts from the paths in your drill config.
2. **Restores** the newest artifact into an ephemeral sandbox directory
   (SQLite databases, `.tar.gz` archives, or plain file trees).
3. **Probes** the restored data: integrity checks, table/row inventories,
   checksum-manifest verification, freshness of the newest record.
4. **Diagnoses** — Claude drives the drill through tools, decides which
   probes matter, digs into anomalies, and distinguishes "corrupt archive"
   from "stale backup" from "healthy".
5. **Verdicts** — emits a schema-validated JSON report
   (`PASS` / `DEGRADED` / `FAIL`) with findings and remediation steps.
   Exit code is non-zero unless the verdict is `PASS`, so it drops straight
   into cron or CI.

Everything runs read-only against your artifacts: the agent's tools can only
write inside the per-drill sandbox, which is deleted afterwards.

## Quickstart

```bash
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...   # or `ant auth login`

# Generate a demo "production" SQLite backup set — one healthy,
# one silently truncated — then drill them:
python -m lazarus.demo
lazarus drill examples/demo-drill.yaml
```

The demo drill shows both sides: the healthy backup passes, and the
truncated one is caught with an explanation of what is missing.

## Drilling your own backups

Describe each backup target in a YAML config:

```yaml
targets:
  - name: orders-db
    kind: sqlite            # sqlite | archive | files
    artifact_glob: /var/backups/orders/*.db
    freshness_hours: 26     # newest artifact older than this is a finding
    expectations:
      min_tables: 5         # optional guardrails the agent checks
      required_tables: [orders, customers]
      row_floor: {orders: 1000}

  - name: uploads
    kind: archive
    artifact_glob: /var/backups/uploads/*.tar.gz
    manifest: /var/backups/uploads/manifest.sha256   # optional
```

Then:

```bash
lazarus drill drill.yaml --report report.json
```

Schedule it nightly; alert on a non-zero exit code. That's a restore drill
program, which is more than most companies with a compliance checkbox have.

## How it's built

- Python, official `anthropic` SDK.
- The drill loop is the SDK **tool runner** (`client.beta.messages.tool_runner`)
  with `@beta_tool`-decorated tools: `list_artifacts`, `restore_artifact`,
  `inspect_sqlite`, `run_readonly_query`, `verify_manifest`, `list_restored_files`.
- Model: `claude-opus-5` with adaptive thinking.
- The final verdict is produced with **structured outputs**
  (`client.messages.parse` + a Pydantic schema), so the report is always
  machine-readable.

## Safety properties

- Tools never touch the live database — only backup artifacts.
- Artifacts are opened read-only; all writes are confined to a
  per-drill temp sandbox that is removed on exit.
- SQL access is a read-only SQLite connection (`mode=ro`) restricted to
  `SELECT`/`PRAGMA`, with row and byte caps on results.
