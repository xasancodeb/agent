"""Restoring backup artifacts into a disposable sandbox.

Nothing in here writes outside the sandbox directory, and source artifacts are
only ever opened for reading — a drill must never be able to damage the thing
it is verifying.
"""

from __future__ import annotations

import glob
import hashlib
import shutil
import sqlite3
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Target


class RestoreError(RuntimeError):
    """Raised when an artifact cannot be restored at all."""


@dataclass
class Artifact:
    path: Path
    size_bytes: int
    modified_epoch: float

    @property
    def age_hours(self) -> float:
        return max(0.0, (time.time() - self.modified_epoch) / 3600.0)

    def describe(self) -> str:
        return (
            f"{self.path} ({self.size_bytes:,} bytes, "
            f"modified {self.age_hours:.1f}h ago)"
        )


def find_artifacts(target: Target) -> list[Artifact]:
    """All artifacts matching the target's glob, newest first."""
    artifacts = []
    for match in glob.glob(target.artifact_glob, recursive=True):
        path = Path(match)
        if not path.is_file() and not (target.kind == "files" and path.is_dir()):
            continue
        stat = path.stat()
        size = stat.st_size
        if path.is_dir():
            size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        artifacts.append(
            Artifact(path=path, size_bytes=size, modified_epoch=stat.st_mtime)
        )
    artifacts.sort(key=lambda a: a.modified_epoch, reverse=True)
    return artifacts


class Sandbox:
    """A temp directory that holds every restore for one drill run."""

    def __init__(self, keep: bool = False):
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="lazarus-drill-"))
        self._restores: dict[str, Path] = {}

    def slot(self, target_name: str) -> Path:
        """A fresh, empty directory for one target's restore."""
        slot = self.root / target_name
        if slot.exists():
            shutil.rmtree(slot)
        slot.mkdir(parents=True)
        return slot

    def record(self, target_name: str, restored_path: Path) -> None:
        self._restores[target_name] = restored_path

    def restored_path(self, target_name: str) -> Path | None:
        return self._restores.get(target_name)

    def contains(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return False
        return True

    def cleanup(self) -> None:
        if self.keep:
            return
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc_info) -> None:
        self.cleanup()


def _safe_extract_tar(archive: Path, dest: Path) -> int:
    """Extract a tar archive, refusing members that escape `dest`."""
    count = 0
    dest_resolved = dest.resolve()
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            if member.issym() or member.islnk():
                # Links can point outside the sandbox after extraction; a
                # restore drill does not need them.
                continue
            target_path = (dest / member.name).resolve()
            if not str(target_path).startswith(str(dest_resolved)):
                raise RestoreError(
                    f"{archive.name}: archive member {member.name!r} escapes the sandbox"
                )
            tar.extract(member, dest, filter="data")
            count += 1
    return count


def _safe_extract_zip(archive: Path, dest: Path) -> int:
    count = 0
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            target_path = (dest / member).resolve()
            if not str(target_path).startswith(str(dest_resolved)):
                raise RestoreError(
                    f"{archive.name}: archive member {member!r} escapes the sandbox"
                )
            zf.extract(member, dest)
            count += 1
    return count


def restore(target: Target, artifact: Artifact, sandbox: Sandbox) -> tuple[Path, str]:
    """Restore one artifact into the sandbox.

    Returns the restored path and a human-readable summary of what happened.
    """
    slot = sandbox.slot(target.name)

    if artifact.size_bytes == 0:
        raise RestoreError(f"{artifact.path.name}: artifact is empty (0 bytes)")

    if target.kind == "sqlite":
        restored = slot / artifact.path.name
        shutil.copy2(artifact.path, restored)
        summary = f"copied {artifact.size_bytes:,} bytes to {restored}"

    elif target.kind == "archive":
        suffixes = "".join(artifact.path.suffixes[-2:]).lower()
        try:
            if suffixes.endswith(".zip") or artifact.path.suffix.lower() == ".zip":
                members = _safe_extract_zip(artifact.path, slot)
            else:
                members = _safe_extract_tar(artifact.path, slot)
        except (tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
            raise RestoreError(
                f"{artifact.path.name}: archive is unreadable — {type(exc).__name__}: {exc}"
            ) from exc
        restored = slot
        summary = f"extracted {members} members into {restored}"

    elif target.kind == "files":
        if artifact.path.is_dir():
            restored = slot / artifact.path.name
            shutil.copytree(artifact.path, restored)
        else:
            restored = slot / artifact.path.name
            shutil.copy2(artifact.path, restored)
        file_count = sum(1 for p in Path(restored).rglob("*") if p.is_file())
        summary = f"copied {file_count} files into {restored}"

    else:  # pragma: no cover - config validation rejects other kinds
        raise RestoreError(f"unsupported target kind {target.kind!r}")

    sandbox.record(target.name, restored)
    return restored, summary


def sqlite_databases(restored: Path) -> list[Path]:
    """SQLite files under a restored path (the path itself, if it is one)."""
    if restored.is_file():
        return [restored]
    candidates = [
        p
        for p in sorted(restored.rglob("*"))
        if p.is_file() and p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
    ]
    return candidates


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """A read-only connection — the drill can never mutate a restore."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
