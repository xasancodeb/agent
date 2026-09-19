"""Generate a demo backup set: one healthy backup, one silently broken one.

    python -m lazarus.demo
    lazarus drill examples/demo-drill.yaml

The point of the demo is that both artifacts look fine from the outside — they
are the right kind of file, in the right place, with a recent timestamp. Only a
restore tells them apart, which is the whole argument for the tool.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

DEFAULT_ROOT = Path("examples")


def _build_orders_db(path: Path, order_count: int) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                total_cents INTEGER NOT NULL,
                placed_at TEXT NOT NULL
            );
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL
            );
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id),
                status TEXT NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY,
                at TEXT NOT NULL,
                what TEXT NOT NULL
            );
            """
        )
        now = time.time()
        conn.executemany(
            "INSERT INTO customers (id, name, email) VALUES (?, ?, ?)",
            [(i, f"Customer {i}", f"customer{i}@example.com") for i in range(1, 301)],
        )
        conn.executemany(
            "INSERT INTO orders (id, customer_id, total_cents, placed_at) VALUES (?, ?, ?, ?)",
            [
                (
                    i,
                    (i % 300) + 1,
                    1500 + (i * 37) % 48000,
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - i * 90)),
                )
                for i in range(1, order_count + 1)
            ],
        )
        conn.executemany(
            "INSERT INTO order_items (id, order_id, sku, quantity) VALUES (?, ?, ?, ?)",
            [(i, (i % order_count) + 1, f"SKU-{i % 120:04d}", (i % 4) + 1) for i in range(1, 4001)],
        )
        conn.executemany(
            "INSERT INTO payments (id, order_id, status) VALUES (?, ?, ?)",
            [
                (i, i, "captured" if i % 11 else "refunded")
                for i in range(1, order_count + 1)
            ],
        )
        conn.executemany(
            "INSERT INTO audit_log (id, at, what) VALUES (?, ?, ?)",
            [
                (i, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - i * 600)), "nightly dump")
                for i in range(1, 51)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _truncate(path: Path, keep_fraction: float = 0.45) -> None:
    """Cut the tail off a file — exactly what an interrupted copy leaves behind."""
    size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.truncate(int(size * keep_fraction))


def build(root: Path = DEFAULT_ROOT) -> Path:
    backups = root / "backups"
    healthy_dir = backups / "orders-healthy"
    broken_dir = backups / "orders-broken"
    uploads_dir = backups / "uploads"
    for directory in (healthy_dir, broken_dir, uploads_dir):
        directory.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d", time.gmtime())

    healthy = healthy_dir / f"orders-{stamp}.db"
    _build_orders_db(healthy, order_count=2400)

    broken = broken_dir / f"orders-{stamp}.db"
    _build_orders_db(broken, order_count=2400)
    _truncate(broken)

    # A file-tree backup with a manifest, where one file was dropped in transit.
    staging = backups / ".uploads-staging"
    if staging.exists():
        for leftover in sorted(staging.rglob("*"), reverse=True):
            leftover.unlink() if leftover.is_file() else leftover.rmdir()
        staging.rmdir()
    staging.mkdir(parents=True)

    files = {}
    for i in range(1, 7):
        payload = (f"upload payload {i}\n" * (40 * i)).encode()
        name = f"upload-{i:02d}.dat"
        (staging / name).write_bytes(payload)
        files[name] = hashlib.sha256(payload).hexdigest()

    # The manifest claims seven files; only six were ever archived.
    files["upload-07.dat"] = hashlib.sha256(b"never archived\n").hexdigest()
    (uploads_dir / "manifest.sha256").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items()))
    )

    archive = uploads_dir / f"uploads-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in sorted(n for n in files if n != "upload-07.dat"):
            tar.add(staging / name, arcname=name)

    for leftover in sorted(staging.rglob("*"), reverse=True):
        leftover.unlink()
    staging.rmdir()

    config = root / "demo-drill.yaml"
    config.write_text(
        f"""# Demo drill: one healthy backup, one truncated, one archive with a
# manifest that over-promises. Generated by `python -m lazarus.demo`.
targets:
  - name: orders-healthy
    kind: sqlite
    artifact_glob: backups/orders-healthy/*.db
    freshness_hours: 26
    expectations:
      min_tables: 5
      required_tables: [orders, customers, payments]
      row_floor:
        orders: 1000
        customers: 100

  - name: orders-broken
    kind: sqlite
    artifact_glob: backups/orders-broken/*.db
    freshness_hours: 26
    expectations:
      min_tables: 5
      required_tables: [orders, customers, payments]
      row_floor:
        orders: 1000
        customers: 100

  - name: uploads
    kind: archive
    artifact_glob: backups/uploads/*.tar.gz
    manifest: backups/uploads/manifest.sha256
    freshness_hours: 26
    expectations:
      min_files: 7
"""
    )

    print(f"Healthy SQLite backup : {healthy} ({healthy.stat().st_size:,} bytes)")
    print(f"Truncated SQLite backup: {broken} ({broken.stat().st_size:,} bytes)")
    print(f"Archive + manifest     : {archive} (manifest lists 7 files, archive holds 6)")
    print(f"Drill config           : {config}")
    print()
    print(f"Next: lazarus drill {config}")
    return config


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    build(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
