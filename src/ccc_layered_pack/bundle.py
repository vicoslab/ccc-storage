"""Simple packset bundle support for future S3/HPC transfer."""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BundleEntry:
    source: str
    arcname: str


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
