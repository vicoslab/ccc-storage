"""Minimal per-node mountd service for Phase 02."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

from ccc_storage_cold.archive import (
    archive_committed_packs_to_cold_storage,
    mirror_committed_packs_to_cold_storage,
    recall_cold_storage_packs,
)
from ccc_storage_cold.config import ColdStorageConfig, hot_pack_dir
from ccc_storage_cold.object_store import ObjectStore
from ccc_storage_cold.policy import archive_decision, needs_recall
from ccc_storage_core.locks import LockHeld, NFSLock
from ccc_storage_core.manifest import (
    VALID_WRITE_POLICIES,
    WRITE_POLICY_LOCAL_SSD_ASYNC,
    WRITE_POLICY_SHARED_NFS,
    ChildManifest,
    OverlayInfo,
    PackInfo,
    PackStack,
    dump_atomic,
    load_manifest,
    normalize_write_policy,
)
from ccc_storage_core.names import safe_namespace_name
from ccc_storage_core.protocol import Request, Response
from ccc_storage_mountd import __version__
from ccc_storage_mountd.childmount import ChildMountError, ChildMountManager
from ccc_storage_mountd.control import ControlServer
from ccc_storage_mountd.dispatcher_fuse import mount_observation_dispatcher
from ccc_storage_mountd.managed_parent import (
    ChildExistsError,
    ChildNotEmptyError,
    ChildNotFoundError,
    ManagedParent,
    ManagedParentError,
)
from ccc_storage_mountd.observation import ObservationError, ObservationManager
from ccc_storage_mountd.overlay import (
    OverlayPaths,
    cleanup_dirty_mirror,
    cleanup_sealed,
    dirty_stats,
    ensure_active_upper,
    latest_dirty_mirror,
    local_overlay_paths,
    seal_active_upper,
)
from ccc_storage_mountd.ownership import Ownership
from ccc_storage_mountd.workers.compaction import (
    build_partial_compaction,
    plan_compaction,
    publish_partial_compaction,
)
from ccc_storage_mountd.workers.levels import (
    LevelPolicy,
    choose_initial_level,
    plan_level_compaction,
)
from ccc_storage_mountd.workers.policy import CommitPolicy, evaluate, overlay_inputs
from ccc_storage_pack.builder import build_delta, pack_object_dir
from ccc_storage_pack.verify import verify_pack

_RUNTIME_BINARIES = (
    "mksquashfs",
    "unsquashfs",
    "squashfuse",
    "fuse-overlayfs",
    "fusermount3",
)


class MountdError(RuntimeError):
    """Mountd service-level error."""


class MountdService:
    """In-process mountd service object used by CLI tests and the real socket."""

    def __init__(
        self,
        nfs_root: str | Path,
        run_dir: str | Path,
        *,
        prefer_kernel: bool = False,
        managed_parent: str | None = None,
        observe_root: str | Path | None = None,
        observe_mountpoint: str | Path | None = None,
        default_write_policy: str = WRITE_POLICY_SHARED_NFS,
        local_overlay_root: str | Path | None = None,
        node_id: str | None = None,
        level_policy: LevelPolicy | None = None,
        storage_uid: int | None = None,
        storage_gid: int | None = None,
        cold_config: ColdStorageConfig | None = None,
        cold_store: ObjectStore | None = None,
    ) -> None:
        self.nfs_root = Path(nfs_root)
        self.run_dir = Path(run_dir)
        self.default_write_policy = normalize_write_policy(default_write_policy)
        self.level_policy = level_policy or LevelPolicy.from_env(os.environ)
        self.ownership = Ownership(uid=storage_uid, gid=storage_gid)
        self.cold_config = cold_config or ColdStorageConfig.from_env(os.environ)
        self.cold_store = cold_store or self.cold_config.build_store()
        self.registry_dir = self.nfs_root / "registry"
        self.mounts = ChildMountManager(
            self.run_dir,
            prefer_kernel=prefer_kernel,
            nfs_root=self.nfs_root,
            local_overlay_root=local_overlay_root,
            node_id=node_id,
            ownership=self.ownership,
        )
        self.children: dict[str, ChildManifest] = {}
        self.manifest_paths: dict[str, Path] = {}
        self.parent: ManagedParent | None = None
        self.observer: ObservationManager | None = None
        self.observe_mountpoint = Path(observe_mountpoint) if observe_mountpoint else None
        if managed_parent:
            self.parent = ManagedParent(
                self.nfs_root,
                self.run_dir,
                parent_path=managed_parent,
                mounts=self.mounts,
                prefer_kernel=prefer_kernel,
                ownership=self.ownership,
                prepare_manifest=self._ensure_hot,
            )
        if observe_root:
            self.observer = ObservationManager(
                self.nfs_root,
                observe_root,
                self.mounts,
                default_write_policy=self.default_write_policy,
                ownership=self.ownership,
                prepare_manifest=self._ensure_hot,
            )

    def reload_registry(self) -> None:
        self.children.clear()
        self.manifest_paths.clear()
        if not self.registry_dir.is_dir():
            return
        for path in sorted(self.registry_dir.rglob("*.toml")):
            try:
                manifest = load_manifest(path)
            except Exception:
                continue
            self.children[manifest.id] = manifest
            self.manifest_paths[manifest.id] = path

    def _find(self, selector: str) -> ChildManifest:
        self.reload_registry()
        selector = selector.strip()
        if selector in self.children:
            return self.children[selector]
        for manifest in self.children.values():
            if selector == manifest.name or selector == manifest.parent_path:
                return manifest
        raise KeyError(selector)

    def handle_ls(self) -> dict[str, Any]:
        self.reload_registry()
        children = [
            self._manifest_status(manifest)
            for manifest in sorted(self.children.values(), key=lambda x: x.id)
        ]
        return {"children": children}

    def handle_status(self, selector: str) -> dict[str, Any]:
        return self._manifest_status(self._find(selector))

    def _manifest_path(self, manifest: ChildManifest) -> Path:
        path = self.manifest_paths.get(manifest.id)
        if path is not None:
            return path
        self.reload_registry()
        path = self.manifest_paths.get(manifest.id)
        if path is not None:
            return path
        raise MountdError(f"manifest path not found for child {manifest.id}")

    def _write_manifest(self, manifest: ChildManifest) -> None:
        path = self._manifest_path(manifest)
        dump_atomic(path, manifest)
        self.ownership.apply(path)

    def _cold_lock_path(self, manifest: ChildManifest) -> Path:
        return self.nfs_root / "locks" / f"{_safe_child_name(manifest.id)}.cold.lock"

    def _require_cold_store(self) -> ObjectStore:
        if self.cold_store is None:
            raise MountdError(
                "cold storage backend is not configured; set CCC_COLD_STORAGE_* / CCC_S3_*"
            )
        return self.cold_store

    @staticmethod
    def _cold_timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _mark_accessed(self, manifest: ChildManifest) -> ChildManifest:
        updated = replace(
            manifest,
            s3=replace(manifest.cold_storage, last_accessed_at=self._cold_timestamp()),
        )
        if updated == manifest:
            return manifest
        self._write_manifest(updated)
        self.reload_registry()
        return self.children.get(updated.id, updated)

    def _ensure_hot(self, manifest: ChildManifest) -> ChildManifest:
        """Recall cold pack bytes before a child is mounted/accessed."""
        if not needs_recall(manifest):
            return self._mark_accessed(manifest)
        with NFSLock(self._cold_lock_path(manifest), op="cold-recall"):
            # Re-read after taking the lock: another node may have recalled it.
            manifest = self._find(manifest.id)
            if not needs_recall(manifest):
                return self._mark_accessed(manifest)
            store = self._require_cold_store()
            hot_dir = hot_pack_dir(self.nfs_root, manifest.id)
            hot_dir.mkdir(parents=True, exist_ok=True)
            self.ownership.apply(hot_dir)
            recalled = recall_cold_storage_packs(
                manifest,
                self._manifest_path(manifest),
                store,
                hot_dir,
            )
            for pack in recalled.pack_stack.lowers:
                self.ownership.apply(pack.path)
            self.ownership.apply(self._manifest_path(recalled))
            self.reload_registry()
            return self.children[recalled.id]

    def handle_mount(self, selector: str) -> dict[str, Any]:
        manifest = self._ensure_hot(self._find(selector))
        self.mounts.mount(manifest)
        return self._manifest_status(manifest)

    def handle_mount_tree(self, selector: str) -> dict[str, Any]:
        """Mount a parent pack and all declared child-boundary packs.

        The parent SquashFS contains only boundary stubs/references. Each child
        manifest is mounted from its own pack stack directly onto the boundary
        directory inside the mounted parent view.
        """
        parent = self._ensure_hot(self._find(selector))
        parent_record = self.mounts.mount(parent)
        nested: list[dict[str, Any]] = []
        for boundary in parent.child_boundaries:
            child = self._ensure_hot(self._find(boundary.child_id))
            boundary_path = boundary.path.strip("/")
            boundary_mountpoint = parent_record.mountpoint / boundary_path
            child_record = self.mounts.mount_at(child, boundary_mountpoint)
            nested.append(
                {
                    "id": child.id,
                    "path": boundary_path,
                    "mountpoint": str(child_record.mountpoint),
                    "mounted": child_record.handle.mounted,
                }
            )
        status = self._manifest_status(parent)
        status["nested_mounts"] = nested
        return status

    def handle_umount(self, selector: str) -> dict[str, Any]:
        manifest = self._find(selector)
        self.mounts.unmount(manifest.id)
        return self._manifest_status(manifest)

    def overlay_paths(self, manifest: ChildManifest) -> OverlayPaths:
        return OverlayPaths.for_child(self.nfs_root / "overlays", manifest.id)

    def handle_commit(self, selector: str, *, message: str = "") -> dict[str, Any]:
        manifest = self._find(selector)
        mount_status = self.mounts.status(manifest)
        if mount_status.get("mounted") and mount_status.get("mode") == "rw":
            raise ChildMountError(
                f"cannot commit {manifest.id} while its writable child view is still mounted; "
                "unmount/drain it first"
            )

        if manifest.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC:
            return self._commit_local_async(selector, message=message)
        return self._commit_shared_nfs(selector, message=message)

    def _commit_shared_nfs(self, selector: str, *, message: str = "") -> dict[str, Any]:
        manifest = self._find(selector)
        paths = self.overlay_paths(manifest)
        ensure_active_upper(paths, self.ownership)
        stats = dirty_stats(paths.active_upper)
        if not stats.dirty:
            return self._manifest_status(replace(manifest, state="clean"))

        lock_path = self.nfs_root / "locks" / f"{_safe_child_name(manifest.id)}.commit.lock"
        with NFSLock(lock_path, op="commit"):
            # Re-resolve after taking the lock, in case another node committed.
            manifest = self._find(selector)
            paths = self.overlay_paths(manifest)
            ensure_active_upper(paths, self.ownership)
            new_generation = manifest.generation + 1
            sealed = seal_active_upper(paths, generation=new_generation, ownership=self.ownership)
            try:
                return self._build_commit_from_source(
                    manifest,
                    sealed.path,
                    new_generation=new_generation,
                    message=message,
                    overlay_mode="shared-overlay",
                    cleanup=lambda: cleanup_sealed(sealed),
                )
            except Exception:
                cleanup_sealed(sealed)
                raise

    def _commit_local_async(self, selector: str, *, message: str = "") -> dict[str, Any]:
        manifest = self._find(selector)
        latest = latest_dirty_mirror(self.nfs_root, manifest.id)
        if latest is None or (latest.file_count == 0 and latest.bytes == 0):
            return self._manifest_status(replace(manifest, state="clean"))
        writer_lock = NFSLock(self.mounts.writer_lock_path(manifest.id), op="commit-local")
        try:
            writer_lock.acquire()
        except LockHeld as exc:
            raise ChildMountError(
                f"cannot commit {manifest.id}: local writer lock is held; "
                "unmount/drain writer first"
            ) from exc
        try:
            lock_path = self.nfs_root / "locks" / f"{_safe_child_name(manifest.id)}.commit.lock"
            with NFSLock(lock_path, op="commit"):
                manifest = self._find(selector)
                latest = latest_dirty_mirror(self.nfs_root, manifest.id)
                if latest is None or (latest.file_count == 0 and latest.bytes == 0):
                    return self._manifest_status(replace(manifest, state="clean"))
                if latest.base_generation != manifest.generation:
                    raise ChildMountError(
                        f"cannot commit {manifest.id}: dirty mirror base generation "
                        f"{latest.base_generation} != manifest generation {manifest.generation}"
                    )
                return self._build_commit_from_source(
                    manifest,
                    latest.path,
                    new_generation=manifest.generation + 1,
                    message=message,
                    overlay_mode=WRITE_POLICY_LOCAL_SSD_ASYNC,
                    cleanup=lambda: self._cleanup_local_async_state(manifest.id),
                )
        finally:
            writer_lock.release()

    def _cleanup_local_async_state(self, manifest_id: str) -> None:
        cleanup_dirty_mirror(self.nfs_root, manifest_id)
        self.mounts.cleanup_local_child(manifest_id)

    def _build_commit_from_source(
        self,
        manifest: ChildManifest,
        source: Path,
        *,
        new_generation: int,
        message: str,
        overlay_mode: str,
        cleanup,
    ) -> dict[str, Any]:
        delta_dir = pack_object_dir(self.nfs_root / "packs", manifest.id)
        delta_dir.mkdir(parents=True, exist_ok=True)
        self.ownership.apply(delta_dir)
        delta_pack = delta_dir / f"delta-g{new_generation:04d}.sqfs"
        build_kwargs: dict[str, Any] = {}
        if self.ownership.uid is not None:
            build_kwargs["uid"] = self.ownership.uid
        if self.ownership.gid is not None:
            build_kwargs["gid"] = self.ownership.gid
        result = build_delta(
            source,
            manifest,
            delta_pack,
            **build_kwargs,
        )
        self.ownership.apply(delta_pack)
        pack = replace(
            result.pack,
            level=choose_initial_level(self.level_policy, result.pack.size),
            generation_min=new_generation,
            generation_max=new_generation,
            kind="delta",
        )
        verify_pack(delta_pack, pack)
        paths = self.overlay_paths(manifest)
        updated = replace(
            manifest,
            generation=new_generation,
            state="clean",
            pack_stack=PackStack(
                active_revision=f"g{new_generation}",
                lowers=(*manifest.pack_stack.lowers, pack),
            ),
            overlay=OverlayInfo(
                mode=overlay_mode,
                active_upper=str(paths.active_upper),
                overlay_generation=manifest.overlay.overlay_generation + 1,
            ),
        )
        manifest_path = self.manifest_paths[manifest.id]
        dump_atomic(manifest_path, updated)
        self.ownership.apply(manifest_path)
        cleanup()
        self.reload_registry()
        committed = self.children[updated.id]
        if self.level_policy.trigger_after_commit:
            compacted = self._compact_if_needed(committed, allow_base=False)
            if compacted is not None:
                committed = compacted
        committed = self._mirror_after_commit_if_configured(committed)
        committed_status = self._manifest_status(committed)
        committed_status["message"] = message
        return committed_status

    def _compact_if_needed(
        self,
        manifest: ChildManifest,
        *,
        allow_base: bool = False,
    ) -> ChildManifest | None:
        candidate = plan_level_compaction(
            manifest.pack_stack.lowers,
            self.level_policy,
            allow_base=allow_base,
        )
        if candidate is None or candidate.blocked_reason:
            return None
        out = self._compaction_output_path(manifest, candidate.target_level, candidate.packs)
        new_pack = build_partial_compaction(
            tuple(candidate.packs),
            out,
            target_level=candidate.target_level,
        )
        self.ownership.apply(out)
        updated, _retired = publish_partial_compaction(
            manifest,
            selected=tuple(candidate.packs),
            new_pack=new_pack,
            target_level=candidate.target_level,
        )
        dump_atomic(self.manifest_paths[manifest.id], updated)
        self.ownership.apply(self.manifest_paths[manifest.id])
        self.reload_registry()
        return self.children[manifest.id]

    def _compaction_output_path(
        self,
        manifest: ChildManifest,
        target_level: int,
        selected: tuple[PackInfo, ...],
    ) -> Path:
        mins = [
            getattr(pack, "generation_min", 0)
            for pack in selected
            if getattr(pack, "generation_min", 0)
        ]
        maxes = [
            getattr(pack, "generation_max", 0)
            for pack in selected
            if getattr(pack, "generation_max", 0)
        ]
        generation_min = min(
            mins,
            default=manifest.generation,
        )
        generation_max = max(
            maxes,
            default=manifest.generation,
        )
        pack_dir = pack_object_dir(self.nfs_root / "packs", manifest.id)
        pack_dir.mkdir(parents=True, exist_ok=True)
        self.ownership.apply(pack_dir)
        name = f"compact-L{target_level}-g{generation_min:04d}-g{generation_max:04d}.sqfs"
        return pack_dir / name

    def handle_compact(
        self,
        selector: str,
        *,
        dry_run: bool = False,
        allow_base: bool = False,
    ) -> dict[str, Any]:
        manifest = self._find(selector)
        candidate = plan_level_compaction(
            manifest.pack_stack.lowers,
            self.level_policy,
            allow_base=allow_base,
        )
        base = {
            "id": manifest.id,
            "dry_run": dry_run,
            "allow_base": allow_base,
            "compacted": False,
            "retired_packs": [],
            "compaction": self._compaction_status(manifest, allow_base=allow_base),
        }
        if candidate is None or dry_run or candidate.blocked_reason:
            return base

        out = self._compaction_output_path(manifest, candidate.target_level, candidate.packs)
        new_pack = build_partial_compaction(
            tuple(candidate.packs),
            out,
            target_level=candidate.target_level,
        )
        self.ownership.apply(out)
        updated, retired = publish_partial_compaction(
            manifest,
            selected=tuple(candidate.packs),
            new_pack=new_pack,
            target_level=candidate.target_level,
        )
        dump_atomic(self.manifest_paths[manifest.id], updated)
        self.ownership.apply(self.manifest_paths[manifest.id])
        self.reload_registry()
        status = self._manifest_status(self.children[manifest.id])
        status.update(
            {
                "dry_run": False,
                "allow_base": allow_base,
                "compacted": True,
                "retired_packs": [pack.path for pack in retired],
            }
        )
        return status

    def handle_pin(self, selector: str, *, pinned: bool) -> dict[str, Any]:
        manifest = self._find(selector)
        updated = replace(manifest, pinned=pinned)
        dump_atomic(self.manifest_paths[manifest.id], updated)
        self.ownership.apply(self.manifest_paths[manifest.id])
        self.reload_registry()
        return self._manifest_status(self.children[updated.id])

    def _assert_clean_for_write_policy_switch(self, manifest: ChildManifest) -> None:
        paths = self.overlay_paths(manifest)
        ensure_active_upper(paths, self.ownership)
        stats = dirty_stats(paths.active_upper)
        if stats.dirty:
            raise ChildMountError(
                f"cannot switch write policy for dirty child {manifest.id}; commit or discard first"
            )
        latest = latest_dirty_mirror(self.nfs_root, manifest.id)
        if latest is not None and latest.file_count > 0:
            raise ChildMountError(
                f"cannot switch write policy for dirty local-async child {manifest.id}; "
                "commit or discard published mirror first"
            )
        local_paths = local_overlay_paths(self.mounts.local_overlay_root, manifest.id)
        local_stats = dirty_stats(local_paths.active_upper)
        if local_stats.dirty:
            raise ChildMountError(
                f"cannot switch write policy for dirty local-async child {manifest.id}; "
                "commit or discard local SSD upper first"
            )

    def handle_write_policy(
        self,
        selector: str,
        *,
        policy: str | None = None,
        remount: bool = False,
    ) -> dict[str, Any]:
        with self.mounts.mount_lock():
            manifest = self._find(selector)
            if policy is None:
                return self._manifest_status(manifest)
            next_policy = normalize_write_policy(policy)
            if manifest.write_policy == next_policy:
                return self._manifest_status(manifest)

            self._assert_clean_for_write_policy_switch(manifest)

            mount_status = self.mounts.status(manifest)
            was_mounted = bool(mount_status.get("mounted"))
            mountpoint = mount_status.get("mountpoint", "")
            mode = mount_status.get("mode", "")
            if was_mounted and not remount:
                raise ChildMountError(
                    f"cannot switch write policy for mounted child {manifest.id}; use --remount"
                )
            if was_mounted:
                self.mounts.force_unmount(manifest.id)

            updated = replace(manifest, write_policy=next_policy)
            dump_atomic(self.manifest_paths[manifest.id], updated)
            self.ownership.apply(self.manifest_paths[manifest.id])
            self.reload_registry()
            updated = self.children[updated.id]

            if was_mounted and mountpoint:
                if mode == "rw":
                    self.mounts.mount_rw_at(
                        updated,
                        mountpoint,
                        require_existing=False,
                        prepare_mountpoint=False,
                        increment_ref=False,
                    )
                elif mode == "ro" and updated.pack_stack.lowers:
                    self.mounts.mount_at(
                        updated,
                        mountpoint,
                        require_existing=False,
                        increment_ref=False,
                    )
            return self._manifest_status(updated)

    def _require_parent(self) -> ManagedParent:
        if self.parent is None:
            raise MountdError("no managed parent configured on this mountd")
        return self.parent

    def handle_parent_ls(self) -> dict[str, Any]:
        return {"children": self._require_parent().list_children()}

    def handle_create(self, name: str) -> dict[str, Any]:
        return self._require_parent().create_child(name)

    def handle_rename(self, old_name: str, new_name: str) -> dict[str, Any]:
        return self._require_parent().rename_child(old_name, new_name)

    def handle_rmdir(self, name: str) -> dict[str, Any]:
        return self._require_parent().remove_child(name)

    def handle_access(self, name: str) -> dict[str, Any]:
        return self._require_parent().access_child(name)

    def _require_observer(self) -> ObservationManager:
        if self.observer is None:
            raise MountdError("no observation root configured on this mountd")
        return self.observer

    def handle_observe_ls(self) -> dict[str, Any]:
        return self._require_observer().list_boundaries()

    def handle_observe_mkdir(self, path: str) -> dict[str, Any]:
        status = self._require_observer().mkdir_child(path)
        self.reload_registry()
        return status

    def handle_observe_access(self, path: str) -> dict[str, Any]:
        return self._require_observer().access_child(path)

    def handle_observe_access_at(self, path: str, mountpoint: str) -> dict[str, Any]:
        return self._require_observer().access_child_at(path, mountpoint)

    def handle_observe_rmdir(self, path: str) -> dict[str, Any]:
        status = self._require_observer().rmdir_child(path)
        self.reload_registry()
        return status

    def handle_observe_rename(self, old_path: str, new_path: str) -> dict[str, Any]:
        status = self._require_observer().rename_child(old_path, new_path)
        self.reload_registry()
        return status

    def handle_doctor(self) -> dict[str, Any]:
        self.reload_registry()
        return {
            "nfs_root": str(self.nfs_root),
            "nfs_root_reachable": self.nfs_root.is_dir(),
            "registry_reachable": self.registry_dir.is_dir(),
            "child_count": len(self.children),
            "active_submount_count": self.mounts.active_count(),
            "observation_mountpoint": str(self.observe_mountpoint or ""),
            "observation_mounted": bool(
                self.observe_mountpoint and _is_mountpoint(self.observe_mountpoint)
            ),
            "runtime": _probe_summary_dict(),
            "default_write_policy": self.default_write_policy,
            "storage_uid": self.ownership.uid,
            "storage_gid": self.ownership.gid,
            "cold_storage": {
                "enabled": self.cold_config.enabled,
                "configured": self.cold_store is not None,
                "archive_enabled": self.cold_config.archive_enabled,
                "backend": self.cold_config.backend,
                "prefix": self.cold_config.prefix,
                "idle_seconds": self.cold_config.idle_seconds,
                "interval_seconds": self.cold_config.interval_seconds,
                "mirror_after_commit": self.cold_config.mirror_after_commit,
                "remove_hot": self.cold_config.remove_hot,
            },
        }

    def reap_idle_mounts(self, ttl: float) -> list[str]:
        """Unmount child mounts whose refcount has been zero for at least *ttl*."""
        if ttl <= 0:
            return []
        return self.mounts.idle_unmount_expired(ttl)

    @staticmethod
    def _mirror_dict(mirror) -> dict[str, Any]:
        return mirror.__dict__ | {"path": str(mirror.path)}

    def publish_dirty_epochs(self) -> list[dict[str, Any]]:
        """Publish async dirty mirrors for locally mounted local-ssd writers."""

        return [self._mirror_dict(mirror) for mirror in self.mounts.publish_all_dirty()]

    def handle_publish(self, selector: str | None = None) -> dict[str, Any]:
        if selector:
            manifest = self._find(selector)
            mirror = self.mounts.publish_dirty(manifest.id)
            return {"published": [] if mirror is None else [self._mirror_dict(mirror)]}
        return {"published": self.publish_dirty_epochs()}

    def _cold_prefix(self, manifest: ChildManifest) -> str:
        return self.cold_config.child_prefix(manifest.id, manifest.generation)

    def _cold_summary(self, manifest: ChildManifest) -> dict[str, Any]:
        cold = manifest.cold_storage
        return {
            "configured": self.cold_store is not None,
            "enabled": self.cold_config.enabled,
            "archive_enabled": self.cold_config.archive_enabled,
            "backend": cold.backend or self.cold_config.backend,
            "mode": cold.mode,
            "pack_state": cold.pack_state,
            "snapshot_state": cold.snapshot_state,
            "pack_generation": cold.pack_generation,
            "mirror_generation": cold.mirror_generation,
            "overlay_generation": cold.overlay_generation,
            "uri": cold.uri,
            "archived_at": cold.archived_at,
            "last_mirrored_at": cold.last_mirrored_at,
            "last_recalled_at": cold.last_recalled_at,
            "last_accessed_at": cold.last_accessed_at,
            "hot_pack_files_present": all(
                Path(pack.path).is_file() for pack in manifest.pack_stack.lowers
            ),
            "needs_recall": needs_recall(manifest),
        }

    def handle_cold_status(self, selector: str) -> dict[str, Any]:
        manifest = self._find(selector)
        return {"id": manifest.id, "cold_storage": self._cold_summary(manifest)}

    def _assert_clean_for_cold_archive(self, manifest: ChildManifest, *, remove_hot: bool) -> None:
        paths = self.overlay_paths(manifest)
        ensure_active_upper(paths, self.ownership)
        stats = dirty_stats(paths.active_upper)
        if stats.dirty:
            raise MountdError(f"cannot archive dirty child {manifest.id}; commit or discard first")
        mount_status = self.mounts.status(manifest)
        if remove_hot and mount_status.get("mounted"):
            raise MountdError(f"cannot evict mounted child {manifest.id}; unmount it first")

    def handle_cold_archive(
        self,
        selector: str,
        *,
        keep_hot: bool = False,
    ) -> dict[str, Any]:
        manifest = self._find(selector)
        remove_hot = not keep_hot
        with NFSLock(self._cold_lock_path(manifest), op="cold-archive"):
            manifest = self._find(selector)
            self._assert_clean_for_cold_archive(manifest, remove_hot=remove_hot)
            store = self._require_cold_store()
            prefix = self._cold_prefix(manifest)
            result = archive_committed_packs_to_cold_storage(
                manifest,
                self._manifest_path(manifest),
                store,
                prefix=prefix,
                remove_hot=remove_hot,
                backend=self.cold_config.backend,
            )
            self.ownership.apply(self._manifest_path(result.manifest))
            self.reload_registry()
            status = self._manifest_status(self.children[result.manifest.id])
            status["cold_storage_action"] = "archive" if remove_hot else "mirror"
            status["cold_storage_uploaded_keys"] = list(result.uploaded_keys)
            status["cold_storage_removed_hot_paths"] = list(result.removed_hot_paths)
            return status

    def handle_cold_recall(self, selector: str) -> dict[str, Any]:
        before = self._find(selector)
        was_cold = needs_recall(before)
        manifest = self._ensure_hot(before)
        status = self._manifest_status(manifest)
        status["cold_storage_action"] = "recall"
        status["cold_storage_recalled"] = was_cold
        return status

    def _mirror_after_commit_if_configured(self, manifest: ChildManifest) -> ChildManifest:
        if not self.cold_config.mirror_after_commit or self.cold_store is None:
            return manifest
        result = mirror_committed_packs_to_cold_storage(
            manifest,
            self._manifest_path(manifest),
            self.cold_store,
            prefix=self._cold_prefix(manifest),
            backend=self.cold_config.backend,
            persist_manifest=True,
        )
        self.ownership.apply(self._manifest_path(result.manifest))
        self.reload_registry()
        return self.children[result.manifest.id]

    def run_cold_storage_once(self) -> list[dict[str, Any]]:
        """Run one safe cold-storage archival pass over eligible children."""
        self.reload_registry()
        results: list[dict[str, Any]] = []
        if not self.cold_config.archive_enabled or self.cold_store is None:
            return results
        for manifest in sorted(self.children.values(), key=lambda item: item.id):
            paths = self.overlay_paths(manifest)
            ensure_active_upper(paths, self.ownership)
            stats = dirty_stats(paths.active_upper)
            mount_status = self.mounts.status(manifest)
            decision = archive_decision(
                manifest,
                dirty=stats.dirty,
                mounted=bool(mount_status.get("mounted")),
                idle_seconds=self.cold_config.idle_seconds,
            )
            if decision.reason == "no-access-metadata":
                self._mark_accessed(manifest)
                continue
            if not decision.eligible:
                continue
            result = self.handle_cold_archive(
                manifest.id,
                keep_hot=not self.cold_config.remove_hot,
            )
            result["cold_storage_idle_age_seconds"] = decision.idle_age_seconds
            results.append(result)
        return results

    def dispatch(self, request: Request) -> Response:
        try:
            if request.command == "ls":
                return Response(ok=True, result=self.handle_ls())
            if request.command == "status":
                return Response(ok=True, result=self.handle_status(request.path))
            if request.command == "mount":
                return Response(ok=True, result=self.handle_mount(request.path))
            if request.command == "mount-tree":
                return Response(ok=True, result=self.handle_mount_tree(request.path))
            if request.command == "umount":
                return Response(ok=True, result=self.handle_umount(request.path))
            if request.command == "commit":
                return Response(
                    ok=True,
                    result=self.handle_commit(
                        request.path,
                        message=str(request.payload.get("message", "")),
                    ),
                )
            if request.command == "compact":
                return Response(
                    ok=True,
                    result=self.handle_compact(
                        request.path,
                        dry_run=bool(request.payload.get("dry_run", False)),
                        allow_base=bool(request.payload.get("allow_base", False)),
                    ),
                )
            if request.command == "publish":
                return Response(ok=True, result=self.handle_publish(request.path or None))
            if request.command == "cold-status":
                return Response(ok=True, result=self.handle_cold_status(request.path))
            if request.command == "cold-archive":
                return Response(
                    ok=True,
                    result=self.handle_cold_archive(
                        request.path,
                        keep_hot=bool(request.payload.get("keep_hot", False)),
                    ),
                )
            if request.command == "cold-recall":
                return Response(ok=True, result=self.handle_cold_recall(request.path))
            if request.command == "pin":
                return Response(
                    ok=True,
                    result=self.handle_pin(
                        request.path,
                        pinned=bool(request.payload.get("pinned", True)),
                    ),
                )
            if request.command == "write-policy":
                return Response(
                    ok=True,
                    result=self.handle_write_policy(
                        request.path,
                        policy=request.payload.get("policy"),
                        remount=bool(request.payload.get("remount", False)),
                    ),
                )
            if request.command == "parent-ls":
                return Response(ok=True, result=self.handle_parent_ls())
            if request.command == "create":
                return Response(ok=True, result=self.handle_create(request.path))
            if request.command == "rename":
                return Response(
                    ok=True,
                    result=self.handle_rename(
                        request.path,
                        str(request.payload.get("to", "")),
                    ),
                )
            if request.command == "rmdir":
                return Response(ok=True, result=self.handle_rmdir(request.path))
            if request.command == "access":
                return Response(ok=True, result=self.handle_access(request.path))
            if request.command == "observe-ls":
                return Response(ok=True, result=self.handle_observe_ls())
            if request.command == "observe-mkdir":
                return Response(ok=True, result=self.handle_observe_mkdir(request.path))
            if request.command == "observe-access":
                return Response(ok=True, result=self.handle_observe_access(request.path))
            if request.command == "doctor":
                return Response(ok=True, result=self.handle_doctor())
            return Response(ok=False, error=f"unknown command: {request.command}", code="EPROTO")
        except KeyError as exc:
            return Response(
                ok=False,
                error=f"managed child not found: {exc.args[0]}",
                code="ENOENT",
            )
        except ChildExistsError as exc:
            return Response(ok=False, error=str(exc), code="EEXIST")
        except ChildNotFoundError as exc:
            return Response(ok=False, error=str(exc), code="ENOENT")
        except ChildNotEmptyError as exc:
            return Response(ok=False, error=str(exc), code="ENOTEMPTY")
        except MountdError as exc:
            return Response(ok=False, error=str(exc), code="EPROTO")
        except ObservationError as exc:
            return Response(ok=False, error=str(exc), code="ENOENT")
        except ManagedParentError as exc:
            return Response(ok=False, error=str(exc), code="EPERM")
        except PermissionError as exc:
            return Response(ok=False, error=str(exc), code="EACCES")
        except ChildMountError as exc:
            return Response(ok=False, error=str(exc), code="EBUSY")
        except Exception as exc:
            return Response(ok=False, error=str(exc), code="EINTERNAL")

    def stop(self) -> None:
        self.mounts.stop_all()

    def _manifest_status(self, manifest: ChildManifest) -> dict[str, Any]:
        mount_status = self.mounts.status(manifest)
        paths = self.overlay_paths(manifest)
        ensure_active_upper(paths, self.ownership)
        latest_mirror = (
            latest_dirty_mirror(self.nfs_root, manifest.id)
            if manifest.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC
            else None
        )
        local_paths = (
            local_overlay_paths(self.mounts.local_overlay_root, manifest.id)
            if manifest.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC
            else None
        )
        local_stats = dirty_stats(local_paths.active_upper) if local_paths else None
        if local_paths is not None and local_stats is not None and local_stats.dirty:
            stats = local_stats
            state = "dirty"
            policy_input_path = local_paths.active_upper
            overlay_info = {
                "active_upper": str(paths.active_upper),
                "local_active_upper": str(local_paths.active_upper),
                "dirty": True,
                "file_count": stats.file_count,
                "bytes": stats.bytes,
                "unpublished_local_dirty": True,
            }
            if latest_mirror is not None:
                overlay_info["dirty_mirror"] = str(latest_mirror.path)
                overlay_info["latest_dirty_epoch"] = latest_mirror.epoch
        elif latest_mirror is not None and latest_mirror.file_count > 0:
            stats = dirty_stats(latest_mirror.path)
            state = "dirty" if stats.dirty else manifest.state
            policy_input_path = latest_mirror.path
            overlay_info = {
                "active_upper": str(paths.active_upper),
                "dirty": stats.dirty,
                "file_count": stats.file_count,
                "bytes": stats.bytes,
                "dirty_mirror": str(latest_mirror.path),
                "latest_dirty_epoch": latest_mirror.epoch,
            }
        else:
            stats = dirty_stats(paths.active_upper)
            state = "dirty" if stats.dirty else manifest.state
            policy_input_path = paths.active_upper
            overlay_info = {
                "active_upper": str(paths.active_upper),
                "dirty": stats.dirty,
                "file_count": stats.file_count,
                "bytes": stats.bytes,
            }
        inputs = overlay_inputs(policy_input_path, now=time.time())
        child_policy = replace(CommitPolicy(), mode=manifest.commit_mode or "auto")
        decision = evaluate(child_policy, inputs)
        delta_count = max(0, len(manifest.pack_stack.lowers) - 1)
        return {
            "id": manifest.id,
            "name": manifest.name,
            "type": manifest.type,
            "state": state,
            "generation": manifest.generation,
            "write_policy": manifest.write_policy,
            "pinned": manifest.pinned,
            "mounted": bool(mount_status["mounted"]),
            "mountpoint": mount_status["mountpoint"],
            "refcount": mount_status["refcount"],
            "packs": [pack.to_dict() for pack in manifest.pack_stack.lowers],
            "delta_count": delta_count,
            "overlay": overlay_info,
            "cold_storage": self._cold_summary(manifest),
            "policy": {
                "mode": manifest.commit_mode or "auto",
                "decision": decision,
            },
            "compaction": {
                **self._legacy_compaction_status(manifest),
                **self._compaction_status(manifest),
            },
        }

    def _legacy_compaction_status(self, manifest: ChildManifest) -> dict[str, Any]:
        comp = plan_compaction(manifest)
        return {
            "legacy_needed": comp is not None,
            "legacy_reason": comp.reason if comp else "",
        }

    def _compaction_status(
        self,
        manifest: ChildManifest,
        *,
        allow_base: bool = False,
    ) -> dict[str, Any]:
        candidate = plan_level_compaction(
            manifest.pack_stack.lowers,
            self.level_policy,
            allow_base=allow_base,
        )
        if candidate is None:
            return {
                "needed": False,
                "reason": "",
                "target_level": None,
                "selected_packs": [],
                "total_bytes": 0,
                "blocked_reason": "",
            }
        return {
            "needed": True,
            "reason": candidate.reason,
            "target_level": candidate.target_level,
            "selected_packs": [pack.path for pack in candidate.packs],
            "total_bytes": candidate.total_bytes,
            "blocked_reason": candidate.blocked_reason,
        }

    def run_background_compaction_once(self) -> list[dict[str, Any]]:
        """Run one safe background compaction pass over eligible children."""
        self.reload_registry()
        results: list[dict[str, Any]] = []
        for manifest in sorted(self.children.values(), key=lambda item: item.id):
            mount_status = self.mounts.status(manifest)
            if mount_status.get("mounted") and mount_status.get("mode") == "rw":
                continue
            candidate = plan_level_compaction(manifest.pack_stack.lowers, self.level_policy)
            if candidate is None or candidate.blocked_reason:
                continue
            results.append(self.handle_compact(manifest.id))
        return results


def _safe_child_name(value: str) -> str:
    return safe_namespace_name(value)


def _probe_summary_dict() -> dict[str, Any]:
    dev_fuse = os.path.exists("/dev/fuse") and os.access("/dev/fuse", os.R_OK | os.W_OK)
    binaries = {name: shutil.which(name) or "" for name in _RUNTIME_BINARIES}
    return {"dev_fuse_rw": dev_fuse, "binaries": binaries}


def _probe_summary() -> list[str]:
    runtime = _probe_summary_dict()
    lines = ["ccc-storage mountd runtime probe (lightweight):"]
    lines.append(f"  /dev/fuse rw      : {'yes' if runtime['dev_fuse_rw'] else 'no'}")
    for name, path in runtime["binaries"].items():
        lines.append(f"  {name:<16}: {path or 'MISSING'}")
    lines.append("note: for the authoritative active probe run `make probe`.")
    return lines


def _is_mountpoint(path: str | Path) -> bool:
    return os.path.ismount(path)


def _wait_for_mountpoint(path: str | Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_mountpoint(path):
            return True
        time.sleep(0.1)
    return _is_mountpoint(path)


def _parse_owner_id(value: object, label: str = "owner id") -> int | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"{label} must be a non-negative integer")
    return parsed


def _env_owner_id(*names: str) -> int | None:
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return _parse_owner_id(value, f"${name}")
    return None


def _write_ready_file(service: MountdService, ready_file: str | Path) -> None:
    path = Path(ready_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(service.handle_doctor(), indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _serve_forever(
    server: ControlServer,
    service: MountdService,
    *,
    ready_file: str | Path | None = None,
    idle_unmount_ttl: float = 0.0,
    idle_reap_interval: float = 30.0,
    dirty_publish_interval: float = 1.0,
    compaction_interval: float = 0.0,
    cold_storage_interval: float = 0.0,
) -> int:
    stop = False
    next_reap = time.monotonic() + max(idle_reap_interval, 0.1)
    next_publish = time.monotonic() + max(dirty_publish_interval, 0.1)
    next_compact = time.monotonic() + max(compaction_interval, 0.1)
    next_cold = time.monotonic() + max(cold_storage_interval, 0.1)

    def _handler(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal stop
        stop = True

    old_int = signal.signal(signal.SIGINT, _handler)
    old_term = signal.signal(signal.SIGTERM, _handler)
    try:
        server.start()
        if ready_file:
            _write_ready_file(service, ready_file)
        while not stop:
            now = time.monotonic()
            if idle_unmount_ttl > 0 and now >= next_reap:
                with contextlib.suppress(Exception):
                    service.reap_idle_mounts(idle_unmount_ttl)
                next_reap = now + max(idle_reap_interval, 0.1)
            if dirty_publish_interval > 0 and now >= next_publish:
                with contextlib.suppress(Exception):
                    service.publish_dirty_epochs()
                next_publish = now + max(dirty_publish_interval, 0.1)
            if compaction_interval > 0 and now >= next_compact:
                with contextlib.suppress(Exception):
                    service.run_background_compaction_once()
                next_compact = now + max(compaction_interval, 0.1)
            if cold_storage_interval > 0 and now >= next_cold:
                with contextlib.suppress(Exception):
                    service.run_cold_storage_once()
                next_cold = now + max(cold_storage_interval, 0.1)
            time.sleep(0.2)
    finally:
        with contextlib.suppress(Exception):
            server.stop()
        with contextlib.suppress(Exception):
            service.stop()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    return 0


def main(argv: list[str] | None = None, *, prog: str = "ccc-storage mountd") -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Per-node CCC storage daemon.",
    )
    parser.add_argument("--version", action="version", version=f"{prog} {__version__}")
    parser.add_argument("--probe", action="store_true", help="print runtime-ingredient summary")
    parser.add_argument("--nfs-root", default=os.environ.get("CCC_NFS_ROOT", ""))
    parser.add_argument("--run-dir", default=os.environ.get("CCC_NODE_RUN_DIR", "/run/ccc-storage"))
    parser.add_argument("--socket", default=os.environ.get("CCC_MOUNTD_SOCK", ""))
    parser.add_argument(
        "--prefer-kernel",
        action="store_true",
        default=os.environ.get("CCC_PREFER_KERNEL", "").lower() in {"1", "true", "yes", "on"},
        help="prefer kernel mount(2) helpers over FUSE helpers where supported",
    )
    parser.add_argument(
        "--socket-mode",
        default=os.environ.get("CCC_MOUNTD_SOCKET_MODE", "0600"),
        help="octal permissions for the control socket (default: 0600)",
    )
    parser.add_argument(
        "--managed-parent",
        default=os.environ.get("CCC_MANAGED_PARENT", ""),
        help="managed parent path whose children this node serves (e.g. /managed/dataset)",
    )
    parser.add_argument(
        "--observe-root",
        default=os.environ.get("CCC_OBSERVE_ROOT", ""),
        help="source tree whose CCC_STORAGE_OBSERVE markers define observed children",
    )
    parser.add_argument(
        "--observe-mountpoint",
        default=os.environ.get("CCC_OBSERVE_MOUNTPOINT", ""),
        help="mount a live pyfuse3 observation dispatcher at this path",
    )
    parser.add_argument(
        "--default-write-policy",
        default=os.environ.get("CCC_DEFAULT_WRITE_POLICY", WRITE_POLICY_SHARED_NFS),
        choices=sorted(VALID_WRITE_POLICIES),
        help="write policy for new children when observe marker has no explicit policy",
    )
    parser.add_argument(
        "--local-overlay-root",
        default=os.environ.get("CCC_LOCAL_OVERLAY_ROOT", ""),
        help="node-local SSD root for local-ssd-async upper/work dirs",
    )
    parser.add_argument(
        "--dirty-publish-interval",
        type=float,
        default=float(os.environ.get("CCC_DIRTY_PUBLISH_INTERVAL", "1")),
        help="seconds between best-effort local-ssd-async dirty mirror publishes",
    )
    parser.add_argument(
        "--compaction-interval",
        type=float,
        default=float(os.environ.get("CCC_COMPACT_INTERVAL_SECONDS", "0")),
        help="seconds between safe background log-structured compaction passes; <=0 disables",
    )
    parser.add_argument(
        "--cold-storage-interval",
        type=float,
        default=float(os.environ.get("CCC_COLD_STORAGE_INTERVAL_SECONDS", "604800")),
        help="seconds between automatic cold-storage archival scans; <=0 disables",
    )
    parser.add_argument(
        "--storage-uid",
        type=_parse_owner_id,
        default=None,
        help="UID to own mountd-created shared storage data (env: CCC_STORAGE_USER_ID or USER_ID)",
    )
    parser.add_argument(
        "--storage-gid",
        type=_parse_owner_id,
        default=None,
        help=(
            "GID to own mountd-created shared storage data "
            "(env: CCC_STORAGE_GROUP_ID or GROUP_ID)"
        ),
    )
    parser.add_argument(
        "--observe-ready-timeout",
        type=float,
        default=float(os.environ.get("CCC_OBSERVE_READY_TIMEOUT", "10")),
        help="seconds to wait for --observe-mountpoint before serving",
    )
    parser.add_argument(
        "--ready-file",
        default=os.environ.get("CCC_MOUNTD_READY_FILE", ""),
        help="write doctor JSON here once the socket is accepting requests",
    )
    parser.add_argument(
        "--idle-unmount-ttl",
        type=float,
        default=float(os.environ.get("CCC_IDLE_UNMOUNT_TTL", "300")),
        help="seconds before idle refcount-zero child mounts are unmounted; <=0 disables",
    )
    parser.add_argument(
        "--idle-reap-interval",
        type=float,
        default=float(os.environ.get("CCC_IDLE_REAP_INTERVAL", "30")),
        help="seconds between idle-mount cleanup passes",
    )
    parser.add_argument("--once-doctor", action="store_true", help="print doctor JSON and exit")
    ns = parser.parse_args(argv)

    if ns.probe:
        print("\n".join(_probe_summary()))
        return 0
    if not ns.nfs_root:
        print(f"{prog}: --nfs-root or $CCC_NFS_ROOT is required")
        return 2
    try:
        if ns.storage_uid is None:
            ns.storage_uid = _env_owner_id("CCC_STORAGE_USER_ID", "USER_ID")
        if ns.storage_gid is None:
            ns.storage_gid = _env_owner_id("CCC_STORAGE_GROUP_ID", "GROUP_ID")
    except argparse.ArgumentTypeError as exc:
        print(f"{prog}: {exc}")
        return 2
    if (ns.storage_uid is None) != (ns.storage_gid is None):
        print(
            f"{prog}: configure both --storage-uid/--storage-gid "
            "or both CCC_STORAGE_USER_ID/CCC_STORAGE_GROUP_ID",
        )
        return 2

    service = MountdService(
        ns.nfs_root,
        ns.run_dir,
        prefer_kernel=bool(ns.prefer_kernel),
        managed_parent=ns.managed_parent or None,
        observe_root=ns.observe_root or None,
        observe_mountpoint=ns.observe_mountpoint or None,
        default_write_policy=ns.default_write_policy,
        local_overlay_root=ns.local_overlay_root or None,
        storage_uid=ns.storage_uid,
        storage_gid=ns.storage_gid,
    )
    service.reload_registry()
    if ns.observe_mountpoint:
        if not ns.observe_root:
            print(f"{prog}: --observe-mountpoint requires --observe-root")
            return 2
        Path(ns.observe_mountpoint).mkdir(parents=True, exist_ok=True)

        def _run_observation_fuse() -> None:
            try:
                mount_observation_dispatcher(service, ns.observe_root, ns.observe_mountpoint)
            except Exception as exc:
                print(f"{prog}: observation dispatcher failed: {exc}", flush=True)
                traceback.print_exc()
                os._exit(3)

        threading.Thread(
            target=_run_observation_fuse,
            name="ccc-storage-observe-fuse",
            daemon=True,
        ).start()
        if ns.observe_ready_timeout > 0 and not _wait_for_mountpoint(
            ns.observe_mountpoint,
            ns.observe_ready_timeout,
        ):
            print(
                f"{prog}: observation mountpoint not ready after "
                f"{ns.observe_ready_timeout:g}s: {ns.observe_mountpoint}",
                flush=True,
            )
            return 3
    if ns.once_doctor:
        print(json.dumps(service.handle_doctor(), indent=2, sort_keys=True))
        return 0
    socket_path = ns.socket or str(Path(ns.run_dir) / "mountd.sock")
    try:
        socket_mode = int(str(ns.socket_mode), 8)
    except ValueError:
        print(f"{prog}: --socket-mode must be an octal mode such as 0600 or 0660")
        return 2
    server = ControlServer(socket_path, service, socket_mode=socket_mode)
    return _serve_forever(
        server,
        service,
        ready_file=ns.ready_file or None,
        idle_unmount_ttl=ns.idle_unmount_ttl,
        idle_reap_interval=ns.idle_reap_interval,
        dirty_publish_interval=ns.dirty_publish_interval,
        compaction_interval=ns.compaction_interval,
        cold_storage_interval=ns.cold_storage_interval,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
