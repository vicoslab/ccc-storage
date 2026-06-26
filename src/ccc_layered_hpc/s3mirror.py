"""Best-effort object-store mirror and cold recall helpers.

S3/object storage is never CCC live truth. These helpers copy committed packs and
manifests out to an object-store abstraction and recall cold packs only after
checksum/size verification.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.manifest import ChildManifest, PackInfo, S3Info, dump_atomic
from ccc_layered_hpc.object_store import LocalObjectStore, ObjectStoreError


class RecallError(RuntimeError):
    """Raised when a cold recall cannot be verified."""


@dataclass(frozen=True)
class MirrorResult:
    manifest: ChildManifest
    uploaded_keys: tuple[str, ...]


def _pack_key(prefix: str, pack: PackInfo) -> str:
    return f"{prefix.strip('/')}/packs/{Path(pack.path).name}"


def mirror_committed_packs(
    manifest: ChildManifest,
    manifest_path: str | Path,
    store: LocalObjectStore,
    *,
    prefix: str,
) -> MirrorResult:
    """Upload pack bytes + manifest bytes best-effort to an object store.

    The passed manifest file is not rewritten: mirror state is advisory and must
    never gate or mutate the authoritative commit path unless the caller chooses
    to persist the returned manifest.
    """
    uploaded: list[str] = []
    for pack in manifest.pack_stack.lowers:
        key = _pack_key(prefix, pack)
        store.put_file(key, pack.path)
        uploaded.append(key)
    manifest_key = f"{prefix.strip('/')}/manifest.toml"
    store.put_file(manifest_key, manifest_path)
    uploaded.append(manifest_key)
    mirrored = replace(
        manifest,
        s3=S3Info(
            pack_state="hot",
            snapshot_state="available",
            pack_generation=manifest.generation,
            overlay_generation=manifest.overlay.overlay_generation,
            uri=prefix.strip("/"),
        ),
    )
    return MirrorResult(manifest=mirrored, uploaded_keys=tuple(uploaded))


def recall_cold_pack(
    manifest: ChildManifest,
    manifest_path: str | Path,
    store: LocalObjectStore,
    hot_dir: str | Path,
) -> ChildManifest:
    """Recall a cold pack stack into *hot_dir*, verify, then atomically publish.

    If any object is missing/corrupt/truncated, no destination pack is published
    and the authoritative manifest is left untouched.
    """
    if not manifest.s3.uri:
        raise RecallError(f"manifest {manifest.id} has no object-store uri")
    out_dir = Path(hot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    recalled: list[PackInfo] = []
    temps: list[Path] = []
    try:
        for pack in manifest.pack_stack.lowers:
            key = _pack_key(manifest.s3.uri, pack)
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
            os.replace(tmp, final)
            temps.remove(tmp)
            recalled.append(replace(pack, path=str(final)))
    except Exception:
        for tmp in temps:
            if tmp.exists():
                tmp.unlink()
        raise

    updated = replace(
        manifest,
        s3=replace(manifest.s3, pack_state="hot"),
        pack_stack=replace(manifest.pack_stack, lowers=tuple(recalled)),
    )
    dump_atomic(manifest_path, updated)
    return updated
