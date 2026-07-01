"""Packset bundle support for S3/HPC transfer."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BundleEntry:
    source: str
    arcname: str


@dataclass(frozen=True)
class MountGraphNode:
    child_id: str
    path: str
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        data = {"child_id": self.child_id, "path": self.path.strip("/") or "."}
        if self.reason:
            data["reason"] = self.reason
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MountGraphNode:
        return cls(
            child_id=str(data["child_id"]),
            path=str(data.get("path", ".")),
            reason=str(data.get("reason", "")),
        )


@dataclass(frozen=True)
class MountGraph:
    root: str
    included: tuple[MountGraphNode, ...] = ()
    excluded: tuple[MountGraphNode, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "included": [node.to_dict() for node in self.included],
            "excluded": [node.to_dict() for node in self.excluded],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MountGraph:
        return cls(
            root=str(data["root"]),
            included=tuple(MountGraphNode.from_dict(item) for item in data.get("included", [])),
            excluded=tuple(MountGraphNode.from_dict(item) for item in data.get("excluded", [])),
        )


@dataclass(frozen=True)
class UnpackedPackset:
    root: Path
    graph: MountGraph


class PacksetVerificationError(RuntimeError):
    """Raised when a packset's checksum file does not match extracted bytes."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_tar_bundle(out: str | Path, entries: list[BundleEntry], manifest: dict) -> Path:
    """Create a tar bundle containing `manifest.json` and selected pack entries."""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w") as tar:
        manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for entry in entries:
            tar.add(entry.source, arcname=entry.arcname)
    return out_path


def build_packset_bundle(
    out: str | Path,
    entries: list[BundleEntry],
    graph: MountGraph,
) -> Path:
    """Build an HPC packset tar with mount graph and checksums."""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    checksums: dict[str, str] = {}
    for entry in entries:
        checksums[entry.arcname] = _sha256_file(Path(entry.source))
    manifest = {"mount_graph": graph.to_dict(), "checksums": checksums}
    with tarfile.open(out_path, "w") as tar:
        manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        checksum_text = "".join(f"{sha}  {name}\n" for name, sha in sorted(checksums.items()))
        checksum_bytes = checksum_text.encode()
        checksum_info = tarfile.TarInfo("checksums.sha256")
        checksum_info.size = len(checksum_bytes)
        tar.addfile(checksum_info, io.BytesIO(checksum_bytes))
        for entry in entries:
            tar.add(entry.source, arcname=entry.arcname)
    return out_path


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if dest_resolved not in (target, *target.parents):
            raise PacksetVerificationError(f"unsafe path in bundle: {member.name}")
    tar.extractall(dest)


def verify_packset_dir(root: str | Path) -> None:
    root_path = Path(root)
    manifest_path = root_path / "manifest.json"
    if not manifest_path.is_file():
        raise PacksetVerificationError("missing manifest.json")
    manifest = json.loads(manifest_path.read_text())
    checksums = manifest.get("checksums", {})
    for arcname, expected in checksums.items():
        path = root_path / arcname
        if not path.is_file():
            raise PacksetVerificationError(f"missing payload: {arcname}")
        actual = _sha256_file(path)
        if actual != expected:
            raise PacksetVerificationError(
                f"checksum mismatch for {arcname}: expected {expected}, got {actual}"
            )


def unpack_packset_bundle(bundle: str | Path, dest: str | Path) -> UnpackedPackset:
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "r") as tar:
        _safe_extract(tar, dest_path)
    verify_packset_dir(dest_path)
    manifest = json.loads((dest_path / "manifest.json").read_text())
    return UnpackedPackset(
        root=dest_path,
        graph=MountGraph.from_dict(manifest["mount_graph"]),
    )
