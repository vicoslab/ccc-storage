"""Minimal per-node mountd service for Phase 02."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

from ccc_layered_core.locks import NFSLock
from ccc_layered_core.manifest import (
    ChildManifest,
    OverlayInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)
from ccc_layered_core.names import safe_namespace_name
from ccc_layered_core.protocol import Request, Response
from ccc_layered_mountd import __version__
from ccc_layered_mountd.childmount import ChildMountError, ChildMountManager
from ccc_layered_mountd.control import ControlServer
from ccc_layered_mountd.dispatcher_fuse import mount_observation_dispatcher
from ccc_layered_mountd.managed_parent import (
    ChildExistsError,
    ChildNotEmptyError,
    ChildNotFoundError,
    ManagedParent,
    ManagedParentError,
)
from ccc_layered_mountd.observation import ObservationError, ObservationManager
from ccc_layered_mountd.overlay import (
    OverlayPaths,
    cleanup_sealed,
    dirty_stats,
    ensure_active_upper,
    seal_active_upper,
)
from ccc_layered_mountd.workers.compaction import plan_compaction
from ccc_layered_mountd.workers.policy import CommitPolicy, evaluate, overlay_inputs
from ccc_layered_pack.builder import build_delta, pack_object_dir
from ccc_layered_pack.verify import verify_pack

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
    ) -> None:
        self.nfs_root = Path(nfs_root)
        self.run_dir = Path(run_dir)
        self.registry_dir = self.nfs_root / "registry"
        self.mounts = ChildMountManager(self.run_dir, prefer_kernel=prefer_kernel)
        self.children: dict[str, ChildManifest] = {}
        self.manifest_paths: dict[str, Path] = {}
        self.parent: ManagedParent | None = None
        self.observer: ObservationManager | None = None
        if managed_parent:
            self.parent = ManagedParent(
                self.nfs_root,
                self.run_dir,
                parent_path=managed_parent,
                mounts=self.mounts,
                prefer_kernel=prefer_kernel,
            )
        if observe_root:
            self.observer = ObservationManager(
                self.nfs_root,
                observe_root,
                self.mounts,
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

    def handle_mount(self, selector: str) -> dict[str, Any]:
        manifest = self._find(selector)
        self.mounts.mount(manifest)
        return self._manifest_status(manifest)

    def handle_mount_tree(self, selector: str) -> dict[str, Any]:
        """Mount a parent pack and all declared child-boundary packs.

        The parent SquashFS contains only boundary stubs/references. Each child
        manifest is mounted from its own pack stack directly onto the boundary
        directory inside the mounted parent view.
        """
        parent = self._find(selector)
        parent_record = self.mounts.mount(parent)
        nested: list[dict[str, Any]] = []
        for boundary in parent.child_boundaries:
            child = self._find(boundary.child_id)
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
        paths = self.overlay_paths(manifest)
        ensure_active_upper(paths)
        stats = dirty_stats(paths.active_upper)
        if not stats.dirty:
            return self._manifest_status(replace(manifest, state="clean"))

        lock_path = self.nfs_root / "locks" / f"{_safe_child_name(manifest.id)}.commit.lock"
        with NFSLock(lock_path, op="commit"):
            # Re-resolve after taking the lock, in case another node committed.
            manifest = self._find(selector)
            paths = self.overlay_paths(manifest)
            ensure_active_upper(paths)
            new_generation = manifest.generation + 1
            sealed = seal_active_upper(paths, generation=new_generation)
            delta_dir = pack_object_dir(self.nfs_root / "packs", manifest.id)
            delta_dir.mkdir(parents=True, exist_ok=True)
            delta_pack = delta_dir / f"delta-g{new_generation:04d}.sqfs"
            result = build_delta(sealed.path, manifest, delta_pack)
            verify_pack(delta_pack, result.pack)
            updated = replace(
                manifest,
                generation=new_generation,
                state="clean",
                pack_stack=PackStack(
                    active_revision=f"g{new_generation}",
                    lowers=(*manifest.pack_stack.lowers, result.pack),
                ),
                overlay=OverlayInfo(
                    mode="shared-overlay",
                    active_upper=str(paths.active_upper),
                    overlay_generation=manifest.overlay.overlay_generation + 1,
                ),
            )
            manifest_path = self.manifest_paths[manifest.id]
            dump_atomic(manifest_path, updated)
            cleanup_sealed(sealed)
            self.reload_registry()
            committed = self.children[updated.id]
            committed_status = self._manifest_status(committed)
            committed_status["message"] = message
            return committed_status

    def handle_pin(self, selector: str, *, pinned: bool) -> dict[str, Any]:
        manifest = self._find(selector)
        updated = replace(manifest, pinned=pinned)
        dump_atomic(self.manifest_paths[manifest.id], updated)
        self.reload_registry()
        return self._manifest_status(self.children[updated.id])

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
        return self._require_observer().mkdir_child(path)

    def handle_observe_access(self, path: str) -> dict[str, Any]:
        return self._require_observer().access_child(path)

    def handle_observe_access_at(self, path: str, mountpoint: str) -> dict[str, Any]:
        return self._require_observer().access_child_at(path, mountpoint)

    def handle_observe_rmdir(self, path: str) -> dict[str, Any]:
        return self._require_observer().rmdir_child(path)

    def handle_observe_rename(self, old_path: str, new_path: str) -> dict[str, Any]:
        return self._require_observer().rename_child(old_path, new_path)

    def handle_doctor(self) -> dict[str, Any]:
        self.reload_registry()
        return {
            "nfs_root": str(self.nfs_root),
            "nfs_root_reachable": self.nfs_root.is_dir(),
            "registry_reachable": self.registry_dir.is_dir(),
            "child_count": len(self.children),
            "active_submount_count": self.mounts.active_count(),
            "runtime": _probe_summary_dict(),
        }

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
            if request.command == "pin":
                return Response(
                    ok=True,
                    result=self.handle_pin(
                        request.path,
                        pinned=bool(request.payload.get("pinned", True)),
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
        ensure_active_upper(paths)
        stats = dirty_stats(paths.active_upper)
        state = "dirty" if stats.dirty else manifest.state
        inputs = overlay_inputs(paths.active_upper, now=time.time())
        child_policy = replace(CommitPolicy(), mode=manifest.commit_mode or "auto")
        decision = evaluate(child_policy, inputs)
        comp = plan_compaction(manifest)
        delta_count = max(0, len(manifest.pack_stack.lowers) - 1)
        return {
            "id": manifest.id,
            "name": manifest.name,
            "type": manifest.type,
            "state": state,
            "generation": manifest.generation,
            "pinned": manifest.pinned,
            "mounted": bool(mount_status["mounted"]),
            "mountpoint": mount_status["mountpoint"],
            "refcount": mount_status["refcount"],
            "packs": [pack.to_dict() for pack in manifest.pack_stack.lowers],
            "delta_count": delta_count,
            "overlay": {
                "active_upper": str(paths.active_upper),
                "dirty": stats.dirty,
                "file_count": stats.file_count,
                "bytes": stats.bytes,
            },
            "policy": {
                "mode": manifest.commit_mode or "auto",
                "decision": decision,
            },
            "compaction": {
                "needed": comp is not None,
                "reason": comp.reason if comp else "",
            },
        }


def _safe_child_name(value: str) -> str:
    return safe_namespace_name(value)


def _probe_summary_dict() -> dict[str, Any]:
    dev_fuse = os.path.exists("/dev/fuse") and os.access("/dev/fuse", os.R_OK | os.W_OK)
    binaries = {name: shutil.which(name) or "" for name in _RUNTIME_BINARIES}
    return {"dev_fuse_rw": dev_fuse, "binaries": binaries}


def _probe_summary() -> list[str]:
    runtime = _probe_summary_dict()
    lines = ["ccc-layered-mountd runtime probe (lightweight):"]
    lines.append(f"  /dev/fuse rw      : {'yes' if runtime['dev_fuse_rw'] else 'no'}")
    for name, path in runtime["binaries"].items():
        lines.append(f"  {name:<16}: {path or 'MISSING'}")
    lines.append("note: for the authoritative active probe run `make probe`.")
    return lines


def _serve_forever(server: ControlServer, service: MountdService) -> int:
    stop = False

    def _handler(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal stop
        stop = True

    old_int = signal.signal(signal.SIGINT, _handler)
    old_term = signal.signal(signal.SIGTERM, _handler)
    try:
        server.start()
        while not stop:
            time.sleep(0.2)
    finally:
        with contextlib.suppress(Exception):
            server.stop()
        with contextlib.suppress(Exception):
            service.stop()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-layered-mountd",
        description="Per-node layered-storage daemon.",
    )
    parser.add_argument("--version", action="version", version=f"ccc-layered-mountd {__version__}")
    parser.add_argument("--probe", action="store_true", help="print runtime-ingredient summary")
    parser.add_argument("--nfs-root", default=os.environ.get("CCC_NFS_ROOT", ""))
    parser.add_argument("--run-dir", default=os.environ.get("CCC_NODE_RUN_DIR", "/run/ccc-layered"))
    parser.add_argument("--socket", default=os.environ.get("CCC_MOUNTD_SOCK", ""))
    parser.add_argument(
        "--managed-parent",
        default=os.environ.get("CCC_MANAGED_PARENT", ""),
        help="managed parent path whose children this node serves (e.g. /managed/dataset)",
    )
    parser.add_argument(
        "--observe-root",
        default=os.environ.get("CCC_OBSERVE_ROOT", ""),
        help="source tree whose CCC_LAYERED_OBSERVE markers define observed children",
    )
    parser.add_argument(
        "--observe-mountpoint",
        default=os.environ.get("CCC_OBSERVE_MOUNTPOINT", ""),
        help="mount a live pyfuse3 observation dispatcher at this path",
    )
    parser.add_argument("--once-doctor", action="store_true", help="print doctor JSON and exit")
    ns = parser.parse_args(argv)

    if ns.probe:
        print("\n".join(_probe_summary()))
        return 0
    if not ns.nfs_root:
        print("ccc-layered-mountd: --nfs-root or $CCC_NFS_ROOT is required")
        return 2

    service = MountdService(
        ns.nfs_root,
        ns.run_dir,
        managed_parent=ns.managed_parent or None,
        observe_root=ns.observe_root or None,
    )
    service.reload_registry()
    if ns.observe_mountpoint:
        if not ns.observe_root:
            print("ccc-layered-mountd: --observe-mountpoint requires --observe-root")
            return 2
        Path(ns.observe_mountpoint).mkdir(parents=True, exist_ok=True)

        def _run_observation_fuse() -> None:
            try:
                mount_observation_dispatcher(service, ns.observe_root, ns.observe_mountpoint)
            except Exception as exc:
                print(f"ccc-layered-mountd: observation dispatcher failed: {exc}", flush=True)
                traceback.print_exc()
                os._exit(3)

        threading.Thread(
            target=_run_observation_fuse,
            name="ccc-layered-observe-fuse",
            daemon=True,
        ).start()
    if ns.once_doctor:
        import json

        print(json.dumps(service.handle_doctor(), indent=2, sort_keys=True))
        return 0
    socket_path = ns.socket or str(Path(ns.run_dir) / "mountd.sock")
    server = ControlServer(socket_path, service)
    return _serve_forever(server, service)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
