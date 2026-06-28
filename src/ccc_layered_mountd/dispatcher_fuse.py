"""pyfuse3 observation-root dispatcher and pure namespace core.

The pure :class:`ObservationDispatchCore` is intentionally testable without a
FUSE-capable host. The real pyfuse3 binding below imports pyfuse3 lazily so unit
validation can run in ordinary containers.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ccc_layered_core.observe import resolve_observed_child

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ccc_layered_mountd.daemon import MountdService
    from ccc_layered_mountd.managed_parent import ManagedParent


class DispatcherUnavailable(RuntimeError):
    """Raised when the pyfuse3 dispatcher cannot be provided on this host."""


@dataclass(frozen=True)
class DispatchEntry:
    path: str
    name: str
    kind: str
    size: int = 0


class ObservationDispatchCore:
    """Pure namespace logic for marker-driven observation FUSE roots.

    The core never recursively scans: listings are immediate-directory only, and
    child data is handed to mountd by asking it to mount the resolved observed
    child at the visible dispatcher path.
    """

    def __init__(
        self,
        source_root: str | Path,
        mount_root: str | Path,
        service: MountdService,
        *,
        materialize_mountpoints: bool = True,
    ):
        self.source_root = Path(source_root)
        self.mount_root = Path(mount_root)
        self.service = service
        self.materialize_mountpoints = materialize_mountpoints

    def validate_name(self, name: str | bytes) -> str:
        if isinstance(name, bytes):
            text = os.fsdecode(name)
        else:
            text = name
        if text in {"", ".", ".."} or "/" in text or "\x00" in text:
            raise ValueError(f"unsafe dispatcher name: {text!r}")
        return text

    def normalize_rel(self, rel_path: str | Path) -> str:
        text = os.fspath(rel_path).strip("/")
        if text == "":
            return ""
        parts = text.split("/")
        if any(part in {"", ".", ".."} or "\x00" in part for part in parts):
            raise ValueError(f"unsafe dispatcher path: {text!r}")
        return "/".join(parts)

    def source_path(self, rel_path: str | Path) -> Path:
        rel = self.normalize_rel(rel_path)
        return self.source_root if rel == "" else self.source_root / rel

    def mount_path(self, rel_path: str | Path) -> Path:
        rel = self.normalize_rel(rel_path)
        return self.mount_root if rel == "" else self.mount_root / rel

    def entry_for(self, rel_path: str | Path) -> DispatchEntry:
        rel = self.normalize_rel(rel_path)
        path = self.source_path(rel)
        name = path.name if rel else ""
        if rel == "":
            return DispatchEntry(path=rel, name=name, kind="dir")
        if path.is_dir():
            return DispatchEntry(path=rel, name=name, kind="dir")
        if path.is_file():
            return DispatchEntry(path=rel, name=name, kind="file", size=path.stat().st_size)
        raise FileNotFoundError(rel)

    def listdir(self, rel_path: str | Path = "") -> list[str]:
        path = self.source_path(rel_path)
        if not path.is_dir():
            raise NotADirectoryError(str(path))
        return sorted(entry.name for entry in path.iterdir())

    def mkdir(self, rel_path: str | Path) -> DispatchEntry:
        rel = self.normalize_rel(rel_path)
        if rel == "":
            raise ValueError("cannot mkdir dispatcher root")
        status = self.service.handle_observe_mkdir(rel)
        # In unit/smoke contexts where mount_root is a real directory, materialise
        # the visible mountpoint. When mount_root is an active FUSE mount this path
        # already exists virtually; recursive mkdir failures are ignored there.
        if self.materialize_mountpoints:
            with contextlib_suppress_oserror():
                self.mount_path(status["parent_path"]).mkdir(parents=True, exist_ok=True)
        return DispatchEntry(path=status["parent_path"], name=status["name"], kind="dir")

    def ensure_mounted_for(self, rel_path: str | Path) -> dict[str, Any] | None:
        rel = self.normalize_rel(rel_path)
        observed = resolve_observed_child(self.source_root, rel)
        if observed is None:
            return None
        mountpoint = self.mount_path(observed.boundary_path)
        if self.materialize_mountpoints:
            with contextlib_suppress_oserror():
                mountpoint.mkdir(parents=True, exist_ok=True)
        return self.service.handle_observe_access_at(rel, str(mountpoint))


    def rmdir(self, rel_path: str | Path) -> dict[str, Any]:
        rel = self.normalize_rel(rel_path)
        if rel == "":
            raise ValueError("cannot remove dispatcher root")
        result = self.service.handle_observe_rmdir(rel)
        if self.materialize_mountpoints:
            with contextlib_suppress_oserror():
                self.mount_path(rel).rmdir()
        return result

    def rename(self, old_path: str | Path, new_path: str | Path) -> dict[str, Any]:
        old = self.normalize_rel(old_path)
        new = self.normalize_rel(new_path)
        if old == "" or new == "":
            raise ValueError("cannot rename dispatcher root")
        result = self.service.handle_observe_rename(old, new)
        if self.materialize_mountpoints:
            with contextlib_suppress_oserror():
                self.mount_path(old).rename(self.mount_path(new))
        return result



class contextlib_suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)


def _import_pyfuse3() -> Any:
    """Import pyfuse3 lazily. Separated out so tests can stub it."""
    import pyfuse3  # type: ignore[import-not-found]

    return pyfuse3


def available() -> bool:
    """True iff a real pyfuse3 dispatcher could be mounted on this host."""
    try:
        _import_pyfuse3()
    except Exception:
        return False
    return True


def _mode_for(entry: DispatchEntry) -> int:
    if entry.kind == "dir":
        return stat.S_IFDIR | 0o755
    return stat.S_IFREG | 0o644


def _now_ns() -> int:
    return int(time.time() * 1_000_000_000)


class ObservationFuseOperations:  # pragma: no cover - exercised by runtime smoke
    """Minimal pyfuse3 operations for an observation-root namespace."""

    # pyfuse3.Operations exposes these as class attributes. We avoid importing
    # pyfuse3 at module import time, so mirror the defaults expected by pyfuse3's
    # session init here.
    supports_dot_lookup = True
    enable_writeback_cache = False
    enable_acl = False

    def __init__(
        self,
        core: ObservationDispatchCore,
        pyfuse3: Any,
        *,
        background_mounts: bool = False,
    ):
        self.core = core
        self.pyfuse3 = pyfuse3
        self.background_mounts = background_mounts
        self._inode_to_path: dict[int, str] = {pyfuse3.ROOT_INODE: ""}
        self._path_to_inode: dict[str, int] = {"": pyfuse3.ROOT_INODE}
        self._next_inode = pyfuse3.ROOT_INODE + 1
        self._mount_lock = threading.Lock()
        self._mounting: set[str] = set()

    def _inode_for(self, rel_path: str) -> int:
        if rel_path not in self._path_to_inode:
            inode = self._next_inode
            self._next_inode += 1
            self._path_to_inode[rel_path] = inode
            self._inode_to_path[inode] = rel_path
        return self._path_to_inode[rel_path]

    def _rel_for_inode(self, inode: int) -> str:
        try:
            return self._inode_to_path[inode]
        except KeyError as exc:
            raise self.pyfuse3.FUSEError(errno.ENOENT) from exc

    def _drop_inode_for(self, rel_path: str) -> None:
        inode = self._path_to_inode.pop(rel_path, None)
        if inode is not None:
            self._inode_to_path.pop(inode, None)

    def _move_inode_for(self, old_path: str, new_path: str) -> None:
        inode = self._path_to_inode.pop(old_path, None)
        if inode is not None:
            self._path_to_inode[new_path] = inode
            self._inode_to_path[inode] = new_path

    def _raise_for_runtime_error(self, exc: RuntimeError) -> None:
        message = str(exc)
        if "already exists" in message:
            code = errno.EEXIST
        elif "mounted" in message:
            code = errno.EBUSY
        elif "dirty" in message or "not empty" in message or "cannot remove" in message:
            code = errno.ENOTEMPTY
        elif "not found" in message or "not an observed" in message:
            code = errno.ENOENT
        else:
            code = errno.EPERM
        raise self.pyfuse3.FUSEError(code) from exc


    def _trigger_mount(self, rel_path: str) -> None:
        if not self.background_mounts:
            self.core.ensure_mounted_for(rel_path)
            return
        with self._mount_lock:
            if rel_path in self._mounting:
                return
            self._mounting.add(rel_path)

        def _mount() -> None:
            try:
                self.core.ensure_mounted_for(rel_path)
            except Exception:
                traceback.print_exc()
            finally:
                with self._mount_lock:
                    self._mounting.discard(rel_path)

        threading.Thread(
            target=_mount,
            name=f"ccc-layered-mount-{rel_path}",
            daemon=True,
        ).start()

    def _attrs(self, entry: DispatchEntry) -> Any:
        attr = self.pyfuse3.EntryAttributes()
        attr.st_ino = self._inode_for(entry.path)
        attr.generation = 0
        attr.entry_timeout = 0.1
        attr.attr_timeout = 0.1
        attr.st_mode = _mode_for(entry)
        attr.st_nlink = 2 if entry.kind == "dir" else 1
        attr.st_uid = os.getuid()
        attr.st_gid = os.getgid()
        attr.st_rdev = 0
        attr.st_size = entry.size
        now = _now_ns()
        attr.st_atime_ns = now
        attr.st_mtime_ns = now
        attr.st_ctime_ns = now
        return attr

    async def getattr(self, inode: int, ctx: object | None = None) -> Any:
        rel = self._rel_for_inode(inode)
        return self._attrs(self.core.entry_for(rel))

    async def lookup(self, parent_inode: int, name: bytes, ctx: object | None = None) -> Any:
        parent = self._rel_for_inode(parent_inode)
        try:
            child_name = self.core.validate_name(name)
            rel = child_name if parent == "" else f"{parent}/{child_name}"
            entry = self.core.entry_for(rel)
        except (ValueError, FileNotFoundError):
            raise self.pyfuse3.FUSEError(errno.ENOENT) from None
        if entry.kind == "dir" and rel:
            # Directory lookup is the lazy mount trigger. mkdir() itself does not
            # mount; a later lookup/stat/open below the child does.
            self._trigger_mount(rel)
        return self._attrs(entry)

    async def opendir(self, inode: int, ctx: object | None = None) -> int:
        rel = self._rel_for_inode(inode)
        if rel:
            self._trigger_mount(rel)
        return inode

    async def readdir(self, inode: int, off: int, token: object) -> None:
        rel = self._rel_for_inode(inode)
        names = [".", "..", *self.core.listdir(rel)]
        for idx, name in enumerate(names[off:], start=off + 1):
            if name == ".":
                child_rel = rel
            elif name == "..":
                child_rel = ""
            elif rel == "":
                child_rel = name
            else:
                child_rel = f"{rel}/{name}"
            try:
                attrs = self._attrs(self.core.entry_for(child_rel))
            except FileNotFoundError:
                continue
            if not self.pyfuse3.readdir_reply(token, os.fsencode(name), attrs, idx):
                break

    async def releasedir(self, fh: int) -> None:
        return None

    async def mkdir(
        self,
        parent_inode: int,
        name: bytes,
        mode: int,
        ctx: object | None = None,
    ) -> Any:
        parent = self._rel_for_inode(parent_inode)
        try:
            child_name = self.core.validate_name(name)
            rel = child_name if parent == "" else f"{parent}/{child_name}"
            entry = self.core.mkdir(rel)
            self._trigger_mount(rel)
        except ValueError:
            raise self.pyfuse3.FUSEError(errno.EINVAL) from None
        except FileExistsError:
            raise self.pyfuse3.FUSEError(errno.EEXIST) from None
        return self._attrs(entry)

    async def rmdir(self, parent_inode: int, name: bytes, ctx: object | None = None) -> None:
        parent = self._rel_for_inode(parent_inode)
        try:
            child_name = self.core.validate_name(name)
            rel = child_name if parent == "" else f"{parent}/{child_name}"
            self.core.rmdir(rel)
            self._drop_inode_for(rel)
        except ValueError:
            raise self.pyfuse3.FUSEError(errno.EINVAL) from None
        except RuntimeError as exc:
            self._raise_for_runtime_error(exc)

    async def rename(
        self,
        parent_inode_old: int,
        name_old: bytes,
        parent_inode_new: int,
        name_new: bytes,
        flags: int,
        ctx: object | None = None,
    ) -> None:
        if flags != 0:
            raise self.pyfuse3.FUSEError(errno.EINVAL)
        parent_old = self._rel_for_inode(parent_inode_old)
        parent_new = self._rel_for_inode(parent_inode_new)
        try:
            old_name = self.core.validate_name(name_old)
            new_name = self.core.validate_name(name_new)
            old_rel = old_name if parent_old == "" else f"{parent_old}/{old_name}"
            new_rel = new_name if parent_new == "" else f"{parent_new}/{new_name}"
            self.core.rename(old_rel, new_rel)
            self._move_inode_for(old_rel, new_rel)
        except ValueError:
            raise self.pyfuse3.FUSEError(errno.EINVAL) from None
        except RuntimeError as exc:
            self._raise_for_runtime_error(exc)


def mount_observation_dispatcher(
    service: MountdService,
    source_root: str | Path,
    mountpoint: str | Path,
    *,
    foreground: bool = True,
) -> None:  # pragma: no cover - runtime smoke covers real path
    pyfuse3 = _import_pyfuse3()
    try:
        import trio
    except Exception as exc:  # pragma: no cover
        raise DispatcherUnavailable("trio is required by pyfuse3 but is not importable") from exc

    core = ObservationDispatchCore(
        source_root,
        mountpoint,
        service,
        materialize_mountpoints=False,
    )

    runtime_ops_cls = type(
        "RuntimeObservationFuseOperations",
        (ObservationFuseOperations, pyfuse3.Operations),
        {},
    )
    ops = runtime_ops_cls(core, pyfuse3, background_mounts=True)
    options = set(pyfuse3.default_options)
    options.add("fsname=ccc-layered-observe")
    options.add("subtype=ccc-layered-observe")
    pyfuse3.init(ops, str(mountpoint), options)
    try:
        trio.run(pyfuse3.main)
    finally:
        pyfuse3.close(unmount=True)


def mount_dispatcher(
    parent: ManagedParent,
    mountpoint: str,
    *,
    foreground: bool = True,
) -> None:
    """Compatibility entry point for the older managed-parent placeholder."""
    try:
        _import_pyfuse3()
    except Exception as exc:
        raise DispatcherUnavailable(
            "pyfuse3 is not importable on this host; the managed-parent FUSE "
            "dispatcher is unavailable."
        ) from exc
    raise DispatcherUnavailable(
        "managed-parent pyfuse3 binding is superseded by marker observation roots; "
        "use mount_observation_dispatcher()."
    )
