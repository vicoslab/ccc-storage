"""TOML manifest schema for CCC layered pack state."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

WRITE_POLICY_SHARED_NFS = "shared-nfs"
WRITE_POLICY_LOCAL_SSD_ASYNC = "local-ssd-async"
VALID_WRITE_POLICIES = frozenset(
    {
        WRITE_POLICY_SHARED_NFS,
        WRITE_POLICY_LOCAL_SSD_ASYNC,
    }
)
_WRITE_POLICY_ALIASES = {
    "": WRITE_POLICY_SHARED_NFS,
    "shared": WRITE_POLICY_SHARED_NFS,
    "shared-overlay": WRITE_POLICY_SHARED_NFS,
    "nfs": WRITE_POLICY_SHARED_NFS,
    "nfs-upper": WRITE_POLICY_SHARED_NFS,
    "local": WRITE_POLICY_LOCAL_SSD_ASYNC,
    "local-async": WRITE_POLICY_LOCAL_SSD_ASYNC,
    "ssd": WRITE_POLICY_LOCAL_SSD_ASYNC,
    "local-ssd": WRITE_POLICY_LOCAL_SSD_ASYNC,
}


class ManifestError(ValueError):
    """Base class for manifest validation/load errors."""


class UnsupportedSchemaVersion(ManifestError):
    """Raised when a manifest is newer than this implementation."""


def normalize_write_policy(value: str | None) -> str:
    """Return canonical write-policy name or raise ``ManifestError``."""

    raw = "" if value is None else str(value).strip().lower()
    policy = _WRITE_POLICY_ALIASES.get(raw, raw)
    if policy not in VALID_WRITE_POLICIES:
        valid = ", ".join(sorted(VALID_WRITE_POLICIES))
        raise ManifestError(f"invalid write_policy {value!r}; expected one of: {valid}")
    return policy


@dataclass(frozen=True)
class PackInfo:
    path: str
    sha256: str
    size: int
    file_count: int | None = None
    block: str = "1M"
    comp: str = "zstd"
    # Log-structured pack-level metadata. Old manifests carry none of these, so
    # the defaults below describe a plain base pack at level 0 with no generation
    # range; ``to_dict`` only emits them when set so old readers stay compatible.
    level: int = 0
    generation_min: int = 0
    generation_max: int = 0
    kind: str = "base"

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
        if self.level:
            data["level"] = self.level
        if self.generation_min:
            data["generation_min"] = self.generation_min
        if self.generation_max:
            data["generation_max"] = self.generation_max
        if self.kind and self.kind != "base":
            data["kind"] = self.kind
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
            level=int(data.get("level", 0)),
            generation_min=int(data.get("generation_min", 0)),
            generation_max=int(data.get("generation_max", 0)),
            kind=str(data.get("kind", "base")),
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
class ColdStorageInfo:
    """Cold-storage state for a child manifest.

    ``backend`` is currently ``s3`` for real deployments, but the manifest table
    is intentionally named ``cold_storage`` so S3 remains just one backend. The
    ``S3Info`` alias below keeps old Python callers/tests source-compatible.
    """

    backend: str = "s3"
    mode: str = ""
    pack_state: str = "missing"
    snapshot_state: str = "unavailable"
    pack_generation: int = 0
    mirror_generation: int = 0
    overlay_generation: int = 0
    uri: str = ""
    archived_at: str = ""
    last_mirrored_at: str = ""
    last_recalled_at: str = ""
    last_accessed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "backend": self.backend,
            "pack_state": self.pack_state,
            "snapshot_state": self.snapshot_state,
            "pack_generation": self.pack_generation,
            "mirror_generation": self.mirror_generation,
            "overlay_generation": self.overlay_generation,
        }
        optional = {
            "mode": self.mode,
            "uri": self.uri,
            "archived_at": self.archived_at,
            "last_mirrored_at": self.last_mirrored_at,
            "last_recalled_at": self.last_recalled_at,
            "last_accessed_at": self.last_accessed_at,
        }
        for key, value in optional.items():
            if value:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ColdStorageInfo:
        if not data:
            return cls()
        return cls(
            backend=str(data.get("backend", "s3")),
            mode=str(data.get("mode", "")),
            pack_state=str(data.get("pack_state", "missing")),
            snapshot_state=str(data.get("snapshot_state", "unavailable")),
            pack_generation=int(data.get("pack_generation", 0)),
            mirror_generation=int(data.get("mirror_generation", data.get("pack_generation", 0))),
            overlay_generation=int(data.get("overlay_generation", 0)),
            uri=str(data.get("uri", "")),
            archived_at=str(data.get("archived_at", "")),
            last_mirrored_at=str(data.get("last_mirrored_at", "")),
            last_recalled_at=str(data.get("last_recalled_at", "")),
            last_accessed_at=str(data.get("last_accessed_at", "")),
        )


S3Info = ColdStorageInfo


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
    write_policy: str = WRITE_POLICY_SHARED_NFS
    pack_stack: PackStack = field(default_factory=PackStack)
    overlay: OverlayInfo = field(default_factory=OverlayInfo)
    # Compatibility field name for older Python callers. New code should use
    # ``manifest.cold_storage`` and manifests serialize this as [cold_storage].
    s3: ColdStorageInfo = field(default_factory=ColdStorageInfo)
    child_boundaries: tuple[ChildBoundary, ...] = ()

    @property
    def cold_storage(self) -> ColdStorageInfo:
        return self.s3

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
        write_policy = normalize_write_policy(self.write_policy)
        if write_policy != WRITE_POLICY_SHARED_NFS:
            data["write_policy"] = write_policy
        data["pack_stack"] = self.pack_stack.to_dict()
        data["overlay"] = self.overlay.to_dict()
        data["cold_storage"] = self.cold_storage.to_dict()
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
            write_policy=normalize_write_policy(
                data.get("write_policy", WRITE_POLICY_SHARED_NFS)
            ),
            pack_stack=PackStack.from_dict(data.get("pack_stack")),
            overlay=OverlayInfo.from_dict(data.get("overlay")),
            s3=ColdStorageInfo.from_dict(data.get("cold_storage") or data.get("s3")),
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
        "write_policy",
    ):
        if key in data:
            lines.append(f"{key} = {_scalar(data[key])}")
    lines.append("")

    for table in ("pack_stack", "overlay", "cold_storage"):
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
