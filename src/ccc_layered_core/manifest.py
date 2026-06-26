"""TOML manifest schema for CCC layered pack state."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class ManifestError(ValueError):
    """Base class for manifest validation/load errors."""


class UnsupportedSchemaVersion(ManifestError):
    """Raised when a manifest is newer than this implementation."""


@dataclass(frozen=True)
class PackInfo:
    path: str
    sha256: str
    size: int
    file_count: int | None = None
    block: str = "1M"
    comp: str = "zstd"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "block": self.block,
            "comp": self.comp,
        }
        if self.file_count is not None:
            data["file_count"] = self.file_count
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PackInfo:
        return cls(
            path=str(data["path"]),
            sha256=str(data["sha256"]),
            size=int(data["size"]),
            file_count=None if data.get("file_count") is None else int(data["file_count"]),
            block=str(data.get("block", "1M")),
            comp=str(data.get("comp", "zstd")),
        )


@dataclass(frozen=True)
class PackStack:
    active_revision: str = ""
    lowers: tuple[PackInfo, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_revision": self.active_revision,
            "lowers": [pack.to_dict() for pack in self.lowers],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PackStack:
        if not data:
            return cls()
        return cls(
            active_revision=str(data.get("active_revision", "")),
            lowers=tuple(PackInfo.from_dict(item) for item in data.get("lowers", [])),
        )


@dataclass(frozen=True)
class OverlayInfo:
    mode: str = "none"
    active_upper: str = ""
    overlay_generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "active_upper": self.active_upper,
            "overlay_generation": self.overlay_generation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OverlayInfo:
        if not data:
            return cls()
        return cls(
            mode=str(data.get("mode", "none")),
            active_upper=str(data.get("active_upper", "")),
            overlay_generation=int(data.get("overlay_generation", 0)),
        )


@dataclass(frozen=True)
class S3Info:
    pack_state: str = "missing"
    snapshot_state: str = "unavailable"
    pack_generation: int = 0
    overlay_generation: int = 0
    uri: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "pack_state": self.pack_state,
            "snapshot_state": self.snapshot_state,
            "pack_generation": self.pack_generation,
            "overlay_generation": self.overlay_generation,
        }
        if self.uri:
            data["uri"] = self.uri
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> S3Info:
        if not data:
            return cls()
        return cls(
            pack_state=str(data.get("pack_state", "missing")),
            snapshot_state=str(data.get("snapshot_state", "unavailable")),
            pack_generation=int(data.get("pack_generation", 0)),
            overlay_generation=int(data.get("overlay_generation", 0)),
            uri=str(data.get("uri", "")),
        )


@dataclass(frozen=True)
class ChildBoundary:
    path: str
    child_id: str
    mount_policy: str = "lazy"
    export_policy: str = "explicit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path.strip("/"),
            "child_id": self.child_id,
            "mount_policy": self.mount_policy,
            "export_policy": self.export_policy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChildBoundary:
        return cls(
            path=str(data["path"]).strip("/"),
            child_id=str(data["child_id"]),
            mount_policy=str(data.get("mount_policy", "lazy")),
            export_policy=str(data.get("export_policy", "explicit")),
        )


@dataclass(frozen=True)
class ChildManifest:
    id: str
    name: str
    type: str
    generation: int
    state: str = "clean"
    schema_version: int = SCHEMA_VERSION
    parent_id: str = ""
    parent_path: str = ""
    created_ts: str = ""
    pinned: bool = False
    commit_mode: str = "auto"
    pack_stack: PackStack = field(default_factory=PackStack)
    overlay: OverlayInfo = field(default_factory=OverlayInfo)
    s3: S3Info = field(default_factory=S3Info)
    child_boundaries: tuple[ChildBoundary, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "generation": self.generation,
            "state": self.state,
        }
        if self.parent_id:
            data["parent_id"] = self.parent_id
        if self.parent_path:
            data["parent_path"] = self.parent_path
        if self.created_ts:
            data["created_ts"] = self.created_ts
        if self.pinned:
            data["pinned"] = self.pinned
        if self.commit_mode and self.commit_mode != "auto":
            data["commit_mode"] = self.commit_mode
        data["pack_stack"] = self.pack_stack.to_dict()
        data["overlay"] = self.overlay.to_dict()
        data["s3"] = self.s3.to_dict()
        if self.child_boundaries:
            data["child_boundary"] = [boundary.to_dict() for boundary in self.child_boundaries]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChildManifest:
        version = int(data.get("schema_version", 1))
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"manifest schema_version {version} is newer than supported {SCHEMA_VERSION}"
            )
        boundaries_raw = data.get("child_boundary", [])
        if isinstance(boundaries_raw, dict):
            boundaries_raw = [boundaries_raw]
        return cls(
            schema_version=version,
            id=str(data.get("id") or data.get("name")),
            name=str(data["name"]),
            type=str(data["type"]),
            generation=int(data.get("generation", 0)),
            state=str(data.get("state", "clean")),
            parent_id=str(data.get("parent_id", "")),
            parent_path=str(data.get("parent_path", "")),
            created_ts=str(data.get("created_ts", "")),
            pinned=bool(data.get("pinned", False)),
            commit_mode=str(data.get("commit_mode", "auto")),
            pack_stack=PackStack.from_dict(data.get("pack_stack")),
            overlay=OverlayInfo.from_dict(data.get("overlay")),
            s3=S3Info.from_dict(data.get("s3")),
            child_boundaries=tuple(ChildBoundary.from_dict(item) for item in boundaries_raw),
        )


def load_manifest(path: str | Path) -> ChildManifest:
    with Path(path).open("rb") as f:
        return ChildManifest.from_dict(tomllib.load(f))


def dump_atomic(path: str | Path, manifest: ChildManifest) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = dumps_manifest(manifest)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    finally:
        if tmp.exists():
            tmp.unlink()


def dumps_manifest(manifest: ChildManifest) -> str:
    data = manifest.to_dict()
    try:
        import tomli_w  # type: ignore[import-not-found]

        return tomli_w.dumps(data)
    except Exception:
        return _dumps_toml_subset(data)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _quote(str(value))


def _emit_scalar_table(lines: list[str], name: str, values: dict[str, Any]) -> None:
    lines.append(f"[{name}]")
    for key, value in values.items():
        if isinstance(value, dict | list):
            continue
        lines.append(f"{key} = {_scalar(value)}")
    lines.append("")


def _dumps_toml_subset(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in (
        "schema_version",
        "id",
        "name",
        "type",
        "generation",
        "state",
        "parent_id",
        "parent_path",
        "created_ts",
        "pinned",
        "commit_mode",
    ):
        if key in data:
            lines.append(f"{key} = {_scalar(data[key])}")
    lines.append("")

    for table in ("pack_stack", "overlay", "s3"):
        if table not in data:
            continue
        _emit_scalar_table(lines, table, data[table])
        if table == "pack_stack":
            for item in data[table].get("lowers", []):
                lines.append("[[pack_stack.lowers]]")
                for key, value in item.items():
                    lines.append(f"{key} = {_scalar(value)}")
                lines.append("")

    for item in data.get("child_boundary", []):
        lines.append("[[child_boundary]]")
        for key, value in item.items():
            lines.append(f"{key} = {_scalar(value)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
