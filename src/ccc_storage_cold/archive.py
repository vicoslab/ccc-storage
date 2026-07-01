"""Cold-storage archive, mirror, and recall helpers.

These helpers operate on committed SquashFS pack stacks. They never make object
storage the live CCC truth: hot mounts still use local/NFS pack files, and cold
recall verifies every object before publishing pack paths back into the manifest.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

from ccc_storage_cold.object_store import ObjectStore, ObjectStoreError
from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, dump_atomic

PACK_STATE_MISSING = "missing"
PACK_STATE_HOT = "hot"
PACK_STATE_COLD = "cold"
SNAPSHOT_AVAILABLE = "available"


class RecallError(RuntimeError):
    """Raised when a cold recall cannot be verified."""


@dataclass(frozen=True)
class MirrorResult:
    manifest: ChildManifest
    uploaded_keys: tuple[str, ...]


@dataclass(frozen=True)
class ColdArchiveResult:
    manifest: ChildManifest
    uploaded_keys: tuple[str, ...]
    removed_hot_paths: tuple[str, ...]


def pack_key(prefix: str, pack: PackInfo) -> str:
    return f"{prefix.strip('/')}/packs/{Path(pack.path).name}"


def manifest_key(prefix: str) -> str:
    return f"{prefix.strip('/')}/manifest.toml"


def _timestamp(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def _manifest_with_cold_state(
    manifest: ChildManifest,
    *,
    prefix: str,
    pack_state: str,
    backend: str = "s3",
    mode: str = "mirror",
    now: float | None = None,
) -> ChildManifest:
    cold = replace(
        manifest.cold_storage,
        backend=backend,
        mode=mode,
        pack_state=pack_state,
        snapshot_state=SNAPSHOT_AVAILABLE,
        pack_generation=manifest.generation,
        mirror_generation=manifest.generation,
        overlay_generation=manifest.overlay.overlay_generation,
        uri=prefix.strip("/"),
    )
    stamp = _timestamp(now)
    if pack_state == PACK_STATE_COLD:
        cold = replace(cold, archived_at=stamp, last_mirrored_at=stamp)
    else:
        cold = replace(cold, last_mirrored_at=stamp)
    return replace(manifest, s3=cold)


def _put_manifest_object(store: ObjectStore, key: str, manifest: ChildManifest) -> None:
    with tempfile.TemporaryDirectory(prefix="ccc-storage-manifest-") as tmp_dir:
        tmp = Path(tmp_dir) / "manifest.toml"
        dump_atomic(tmp, manifest)
        store.put_file(key, tmp)


def mirror_committed_packs_to_cold_storage(
    manifest: ChildManifest,
    manifest_path: str | Path,
    store: ObjectStore,
    *,
    prefix: str,
    backend: str = "s3",
    persist_manifest: bool = True,
) -> MirrorResult:
    """Upload committed pack bytes and a hot-state manifest to cold storage.

    Mirror mode keeps hot/NFS pack files and records that object storage has a
    current snapshot. It is suitable for HPC exchange and durability sync.
    """
    uploaded: list[str] = []
    for pack in manifest.pack_stack.lowers:
        key = pack_key(prefix, pack)
        store.put_file(key, pack.path)
        uploaded.append(key)
    mirrored = _manifest_with_cold_state(
        manifest,
        prefix=prefix,
        pack_state=PACK_STATE_HOT,
        backend=backend,
        mode="mirror",
    )
    key = manifest_key(prefix)
    _put_manifest_object(store, key, mirrored)
    uploaded.append(key)
    if persist_manifest:
        dump_atomic(manifest_path, mirrored)
    return MirrorResult(manifest=mirrored, uploaded_keys=tuple(uploaded))


def mirror_committed_packs(
    manifest: ChildManifest,
    manifest_path: str | Path,
    store: ObjectStore,
    *,
    prefix: str,
) -> MirrorResult:
    """Backward-compatible mirror helper.

    Historically this helper was best-effort and did not rewrite the local
    manifest. Preserve that behavior for HPC callers that still import the old
    S3 mirror API.
    """
    return mirror_committed_packs_to_cold_storage(
        manifest,
        manifest_path,
        store,
        prefix=prefix,
        persist_manifest=False,
    )


def archive_committed_packs_to_cold_storage(
    manifest: ChildManifest,
    manifest_path: str | Path,
    store: ObjectStore,
    *,
    prefix: str,
    remove_hot: bool = True,
    backend: str = "s3",
) -> ColdArchiveResult:
    """Upload committed packs and optionally evict hot/NFS pack files.

    All pack objects and a manifest snapshot are uploaded first. Only after every
    upload succeeds is the authoritative manifest rewritten and, when requested,
    hot pack files removed from local/NFS storage.
    """
    uploaded: list[str] = []
    for pack in manifest.pack_stack.lowers:
        key = pack_key(prefix, pack)
        store.put_file(key, pack.path)
        uploaded.append(key)

    archived = _manifest_with_cold_state(
        manifest,
        prefix=prefix,
        pack_state=PACK_STATE_COLD if remove_hot else PACK_STATE_HOT,
        backend=backend,
        mode="archive" if remove_hot else "mirror",
    )
    key = manifest_key(prefix)
    _put_manifest_object(store, key, archived)
    uploaded.append(key)

    dump_atomic(manifest_path, archived)
    removed: list[str] = []
    if remove_hot:
        for pack in manifest.pack_stack.lowers:
            path = Path(pack.path)
            if path.exists():
                path.unlink()
                removed.append(str(path))
    return ColdArchiveResult(
        manifest=archived,
        uploaded_keys=tuple(uploaded),
        removed_hot_paths=tuple(removed),
    )


def recall_cold_storage_packs(
    manifest: ChildManifest,
    manifest_path: str | Path,
    store: ObjectStore,
    hot_dir: str | Path,
) -> ChildManifest:
    """Recall a cold pack stack into *hot_dir*, verify, then atomically publish.

    If any object is missing/corrupt/truncated, no destination pack is published
    and the authoritative manifest is left untouched.
    """
    cold = manifest.cold_storage
    if not cold.uri:
        raise RecallError(f"manifest {manifest.id} has no cold-storage uri")
    out_dir = Path(hot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    recalled: list[PackInfo] = []
    temps: list[Path] = []
    staged: list[tuple[Path, Path, PackInfo]] = []
    try:
        for pack in manifest.pack_stack.lowers:
            key = pack_key(cold.uri, pack)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{Path(pack.path).name}.", suffix=".tmp", dir=out_dir
            )
            os.close(fd)
            tmp = Path(tmp_name)
            temps.append(tmp)
            try:
                store.get_file(key, tmp)
            except ObjectStoreError as exc:
                raise RecallError(str(exc)) from exc
            actual_sha = sha256_file(tmp)
            actual_size = tmp.stat().st_size
            if actual_sha != pack.sha256 or actual_size != pack.size:
                raise RecallError(
                    f"recall verification failed for {key}: expected {pack.sha256}/{pack.size}, "
                    f"got {actual_sha}/{actual_size}"
                )
            final = out_dir / Path(pack.path).name
            staged.append((tmp, final, pack))

        for tmp, final, pack in staged:
            os.replace(tmp, final)
            temps.remove(tmp)
            recalled.append(replace(pack, path=str(final)))
    except Exception:
        for tmp in temps:
            if tmp.exists():
                tmp.unlink()
        raise

    updated_cold = replace(
        cold,
        pack_state=PACK_STATE_HOT,
        mode="recalled",
        last_recalled_at=_timestamp(),
        last_accessed_at=_timestamp(),
    )
    updated = replace(
        manifest,
        s3=updated_cold,
        pack_stack=replace(manifest.pack_stack, lowers=tuple(recalled)),
    )
    dump_atomic(manifest_path, updated)
    return updated


def recall_cold_pack(
    manifest: ChildManifest,
    manifest_path: str | Path,
    store: ObjectStore,
    hot_dir: str | Path,
) -> ChildManifest:
    """Backward-compatible recall helper for old S3 mirror imports."""
    return recall_cold_storage_packs(manifest, manifest_path, store, hot_dir)
