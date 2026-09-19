"""Drill configuration: what to restore and what to expect afterwards."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

KINDS = ("sqlite", "archive", "files")


class ConfigError(ValueError):
    """Raised when a drill config is malformed."""


@dataclass
class Expectations:
    """Guardrails the agent checks the restored data against.

    Every field is optional; an empty Expectations means "just tell me whether
    it restores and looks sane".
    """

    min_tables: int | None = None
    required_tables: list[str] = field(default_factory=list)
    row_floor: dict[str, int] = field(default_factory=dict)
    min_files: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], where: str) -> "Expectations":
        unknown = set(raw) - {"min_tables", "required_tables", "row_floor", "min_files"}
        if unknown:
            raise ConfigError(f"{where}: unknown expectation keys {sorted(unknown)}")
        row_floor = raw.get("row_floor") or {}
        if not isinstance(row_floor, dict):
            raise ConfigError(f"{where}: row_floor must be a mapping of table -> minimum rows")
        return cls(
            min_tables=raw.get("min_tables"),
            required_tables=list(raw.get("required_tables") or []),
            row_floor={str(k): int(v) for k, v in row_floor.items()},
            min_files=raw.get("min_files"),
        )

    def describe(self) -> str:
        parts = []
        if self.min_tables is not None:
            parts.append(f"at least {self.min_tables} tables")
        if self.required_tables:
            parts.append("tables present: " + ", ".join(self.required_tables))
        if self.row_floor:
            parts.append(
                "row floors: "
                + ", ".join(f"{t} >= {n}" for t, n in sorted(self.row_floor.items()))
            )
        if self.min_files is not None:
            parts.append(f"at least {self.min_files} files")
        return "; ".join(parts) if parts else "none declared"


@dataclass
class Target:
    """One backup stream to drill."""

    name: str
    kind: str
    artifact_glob: str
    freshness_hours: float | None = None
    manifest: str | None = None
    expectations: Expectations = field(default_factory=Expectations)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "Target":
        where = f"targets[{index}]"
        name = raw.get("name")
        if not name:
            raise ConfigError(f"{where}: 'name' is required")
        where = f"target '{name}'"

        kind = raw.get("kind")
        if kind not in KINDS:
            raise ConfigError(f"{where}: 'kind' must be one of {', '.join(KINDS)}, got {kind!r}")

        artifact_glob = raw.get("artifact_glob")
        if not artifact_glob:
            raise ConfigError(f"{where}: 'artifact_glob' is required")

        freshness = raw.get("freshness_hours")
        if freshness is not None:
            freshness = float(freshness)
            if freshness <= 0:
                raise ConfigError(f"{where}: 'freshness_hours' must be positive")

        expectations = Expectations.from_dict(raw.get("expectations") or {}, where)
        if kind != "sqlite" and (expectations.required_tables or expectations.row_floor):
            raise ConfigError(
                f"{where}: table and row expectations only apply to kind 'sqlite'"
            )

        return cls(
            name=str(name),
            kind=kind,
            artifact_glob=str(artifact_glob),
            freshness_hours=freshness,
            manifest=str(raw["manifest"]) if raw.get("manifest") else None,
            expectations=expectations,
        )


@dataclass
class DrillConfig:
    targets: list[Target]
    source: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "DrillConfig":
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: top level must be a mapping with a 'targets' key")

        targets = raw.get("targets")
        if not targets:
            raise ConfigError(f"{path}: no targets defined")
        if not isinstance(targets, list):
            raise ConfigError(f"{path}: 'targets' must be a list")

        parsed = [Target.from_dict(t, i) for i, t in enumerate(targets)]
        names = [t.name for t in parsed]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ConfigError(f"{path}: duplicate target names {sorted(duplicates)}")

        # Relative globs resolve against the config file's directory, so a
        # config is portable alongside the backups it describes.
        base = path.parent
        for target in parsed:
            if not Path(target.artifact_glob).is_absolute():
                target.artifact_glob = str(base / target.artifact_glob)
            if target.manifest and not Path(target.manifest).is_absolute():
                target.manifest = str(base / target.manifest)

        return cls(targets=parsed, source=path)

    def get(self, name: str) -> Target:
        for target in self.targets:
            if target.name == name:
                return target
        known = ", ".join(t.name for t in self.targets)
        raise KeyError(f"unknown target {name!r}; configured targets: {known}")
