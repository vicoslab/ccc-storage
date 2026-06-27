"""S3 exchange helpers for external-HPC packsets and output deltas.

These helpers validate the S3-side communication artifacts without requiring a
live external HPC. CCC remains the source of truth; S3 carries immutable input
packsets and output delta/provenance records for later review/import.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from ccc_layered_core.checksum import sha256_file
from ccc_layered_hpc.importqueue import ImportQueue, ImportRecord, Provenance
from ccc_layered_hpc.object_store import ObjectStore, ObjectStoreError


@dataclass(frozen=True)
class HpcPacksetRecord:
    site: str
    root_id: str
    generation: int
    bundle_key: str
    sha256: str
    size: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> HpcPacksetRecord:
        return cls(
            site=str(data["site"]),
            root_id=str(data["root_id"]),
            generation=int(data["generation"]),
            bundle_key=str(data["bundle_key"]),
            sha256=str(data["sha256"]),
            size=int(data["size"]),
        )


@dataclass(frozen=True)
class HpcImportDeltaRecord:
    branch: str
    delta_key: str
    sha256: str
    size: int
    provenance: Provenance

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "delta_key": self.delta_key,
            "sha256": self.sha256,
            "size": self.size,
            "provenance": asdict(self.provenance),
        }

    @classmethod
    def from_dict(cls, data: dict) -> HpcImportDeltaRecord:
        return cls(
            branch=str(data["branch"]),
            delta_key=str(data["delta_key"]),
            sha256=str(data["sha256"]),
            size=int(data["size"]),
            provenance=Provenance(**data["provenance"]),
        )


def publish_hpc_packset_bundle(
    store: ObjectStore,
    bundle: str | Path,
    *,
    prefix: str,
    site: str,
    root_id: str,
    generation: int,
) -> HpcPacksetRecord:
    """Upload an immutable input packset and an exchange record to S3."""
    bundle_path = Path(bundle)
    if not bundle_path.is_file():
        raise FileNotFoundError(bundle_path)
    clean = prefix.strip("/")
    bundle_key = f"{clean}/packset.tar"
    store.put_file(bundle_key, bundle_path)
    record = HpcPacksetRecord(
        site=site,
        root_id=root_id,
        generation=generation,
        bundle_key=bundle_key,
        sha256=sha256_file(bundle_path),
        size=bundle_path.stat().st_size,
    )
    _put_json(store, f"{clean}/exchange-record.json", record.to_dict())
    return record


def fetch_hpc_packset_bundle(
    store: ObjectStore,
    record: HpcPacksetRecord,
    dest: str | Path,
) -> Path:
    """Download and verify an external-HPC input packset bundle."""
    out = Path(dest)
    store.get_file(record.bundle_key, out)
    _verify_file(out, expected_sha=record.sha256, expected_size=record.size)
    return out


def publish_hpc_import_delta(
    store: ObjectStore,
    delta_bundle: str | Path,
    *,
    prefix: str,
    branch: str,
    provenance: Provenance,
) -> HpcImportDeltaRecord:
    """Upload an external-HPC output delta plus provenance metadata."""
    delta_path = Path(delta_bundle)
    if not delta_path.is_file():
        raise FileNotFoundError(delta_path)
    clean = prefix.strip("/")
    delta_key = f"{clean}/output-delta.tar"
    store.put_file(delta_key, delta_path)
    record = HpcImportDeltaRecord(
        branch=branch,
        delta_key=delta_key,
        sha256=sha256_file(delta_path),
        size=delta_path.stat().st_size,
        provenance=provenance,
    )
    _put_json(store, f"{clean}/import-record.json", record.to_dict())
    return record


def import_hpc_delta_from_s3(
    store: ObjectStore,
    prefix: str,
    import_queue: ImportQueue,
) -> ImportRecord:
    """Download an S3 import delta, verify metadata, and enqueue a review branch."""
    clean = prefix.strip("/")
    try:
        record = HpcImportDeltaRecord.from_dict(
            json.loads(store.read_bytes(f"{clean}/import-record.json"))
        )
    except ObjectStoreError:
        raise
    except Exception as exc:
        msg = f"invalid import record at {clean}/import-record.json: {exc}"
        raise ObjectStoreError(msg) from exc

    with tempfile.TemporaryDirectory(prefix="ccc-layered-hpc-import-") as tmp_dir:
        delta = Path(tmp_dir) / "output-delta.tar"
        store.get_file(record.delta_key, delta)
        _verify_file(delta, expected_sha=record.sha256, expected_size=record.size)
        return import_queue.enqueue_delta(
            delta,
            branch=record.branch,
            provenance=record.provenance,
        )


def _put_json(store: ObjectStore, key: str, data: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="ccc-layered-hpc-json-") as tmp_dir:
        path = Path(tmp_dir) / "record.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
        store.put_file(key, path)


def _verify_file(path: Path, *, expected_sha: str, expected_size: int) -> None:
    actual_sha = sha256_file(path)
    actual_size = path.stat().st_size
    if actual_sha != expected_sha or actual_size != expected_size:
        raise ObjectStoreError(
            f"download verification failed for {path}: expected {expected_sha}/{expected_size}, "
            f"got {actual_sha}/{actual_size}"
        )
