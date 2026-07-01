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

from ccc_storage_core.observe import resolve_observed_child
from ccc_storage_mountd.ownership import Ownership

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ccc_storage_mountd.daemon import MountdService
    from ccc_storage_mountd.managed_parent import ManagedParent


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
        self.ownership = getattr(service, "ownership", Ownership())
        self.materialize_mountpoints = materialize_mountpoints
        self.reserved_name = ".ccc-storage"

    def _service_observe_path(self, rel_path: str) -> str:
        if getattr(self.service, "observation_router", None) is not None:
            return str(self.mount_path(rel_path))
        return rel_path

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

    def _contains_reserved(self, rel_path: str) -> bool:
        return any(part == self.reserved_name for part in rel_path.split("/") if part)

    def _private_child_path(self, rel_path: str) -> Path | None:
        rel = self.normalize_rel(rel_path)
        if rel == "" or self._contains_reserved(rel):
            return None
        _boundary, _, inner = rel.partition("/")
        try:
            status = self.service.handle_observe_access_private(self._service_observe_path(rel))
        except RuntimeError as exc:
            message = str(exc)
            if (
                "not registered" in message
                or "not found" in message
                or "not under" in message
                or "no observation root" in message
            ):
                return None
            raise
        mountpoint = status.get("mountpoint")
        if not mountpoint:
            return None
        mounted = Path(mountpoint)
        return mounted if not inner else mounted / inner

    def source_path(self, rel_path: str | Path) -> Path:
        rel = self.normalize_rel(rel_path)
        private_path = self._private_child_path(rel)
        if private_path is not None:
            return private_path
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
        if self._contains_reserved(rel):
            raise FileNotFoundError(rel)
        if path.is_dir():
            return DispatchEntry(path=rel, name=name, kind="dir")
        if path.is_file():
            return DispatchEntry(path=rel, name=name, kind="file", size=path.stat().st_size)
        raise FileNotFoundError(rel)

    def listdir(self, rel_path: str | Path = "") -> list[str]:
        path = self.source_path(rel_path)
        if not path.is_dir():
            raise NotADirectoryError(str(path))
        return sorted(entry.name for entry in path.iterdir() if entry.name != self.reserved_name)

    def mkdir(self, rel_path: str | Path) -> DispatchEntry:
        rel = self.normalize_rel(rel_path)
        if rel == "":
            raise ValueError("cannot mkdir dispatcher root")
        if self._contains_reserved(rel):
            raise ValueError(f"reserved dispatcher path: {rel}")
        if "/" in rel:
            path = self.source_path(rel)
            path.mkdir()
            self.ownership.apply(path)
            return DispatchEntry(path=rel, name=path.name, kind="dir")
        status = self.service.handle_observe_mkdir(self._service_observe_path(rel))
        # In unit/smoke contexts where mount_root is a real directory, materialise
        # the visible mountpoint. When mount_root is an active FUSE mount this path
        # already exists virtually; recursive mkdir failures are ignored there.
        if self.materialize_mountpoints:
            with contextlib_suppress_oserror():
                mountpoint = self.mount_path(status["parent_path"])
                mountpoint.mkdir(parents=True, exist_ok=True)
                self.ownership.apply(mountpoint)
        return DispatchEntry(path=status["parent_path"], name=status["name"], kind="dir")

    def read(self, rel_path: str | Path, *, size: int, offset: int = 0) -> bytes:
        rel = self.normalize_rel(rel_path)
        if self._contains_reserved(rel):
            raise FileNotFoundError(rel)
        path = self.source_path(rel)
        if not path.is_file():
            raise FileNotFoundError(rel)
        with path.open("rb") as fh:
            fh.seek(offset)
            return fh.read(size)

    def ensure_mounted_for(self, rel_path: str | Path) -> dict[str, Any] | None:
        rel = self.normalize_rel(rel_path)
        if getattr(self.service, "observation_router", None) is not None:
            if rel == "" or self._contains_reserved(rel):
                return None
            boundary = rel.split("/", 1)[0]
            mountpoint = self.mount_path(boundary)
            if self.materialize_mountpoints:
                with contextlib_suppress_oserror():
                    mountpoint.mkdir(parents=True, exist_ok=True)
                    self.ownership.apply(mountpoint)
            try:
                return self.service.handle_observe_access_at(
                    self._service_observe_path(rel),
                    str(mountpoint),
                )
            except RuntimeError as exc:
                message = str(exc)
                if "not registered" in message or "not found" in message:
                    return None
                raise
        observed = resolve_observed_child(self.source_root, rel)
        if observed is None:
            return None
        mountpoint = self.mount_path(observed.boundary_path)
        if self.materialize_mountpoints:
            with contextlib_suppress_oserror():
                mountpoint.mkdir(parents=True, exist_ok=True)
                self.ownership.apply(mountpoint)
        try:
            return self.service.handle_observe_access_at(
                self._service_observe_path(rel),
                str(mountpoint),
            )
        except RuntimeError as exc:
            message = str(exc)
            if "not registered" in message or "not found" in message:
                return None
            raise


    def rmdir(self, rel_path: str | Path) -> dict[str, Any]:
        rel = self.normalize_rel(rel_path)
        if rel == "":
            raise ValueError("cannot remove dispatcher root")
        result = self.service.handle_observe_rmdir(self._service_observe_path(rel))
        if self.materialize_mountpoints:
            with contextlib_suppress_oserror():
                self.mount_path(rel).rmdir()
        return result

    def rename(self, old_path: str | Path, new_path: str | Path) -> dict[str, Any]:
        old = self.normalize_rel(old_path)
        new = self.normalize_rel(new_path)
        if old == "" or new == "":
            raise ValueError("cannot rename dispatcher root")
        result = self.service.handle_observe_rename(
            self._service_observe_path(old),
            self._service_observe_path(new),
        )
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
        self._next_fh = 1
        self._fh_to_fd: dict[int, int] = {}
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
            name=f"ccc-storage-mount-{rel_path}",
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
        attr.st_uid = self.core.ownership.attr_uid
        attr.st_gid = self.core.ownership.attr_gid
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
        return self._attrs(entry)

    async def opendir(self, inode: int, ctx: object | None = None) -> int:
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

    async def open(self, inode: int, flags: int, ctx: object | None = None) -> Any:
        rel = self._rel_for_inode(inode)
        try:
            entry = self.core.entry_for(rel)
        except FileNotFoundError:
            raise self.pyfuse3.FUSEError(errno.ENOENT) from None
        if entry.kind != "file":
            raise self.pyfuse3.FUSEError(errno.EISDIR)
        try:
            fd = os.open(self.core.source_path(rel), flags)
        except OSError as exc:
            raise self.pyfuse3.FUSEError(exc.errno) from exc
        fh = self._next_fh
        self._next_fh += 1
        self._fh_to_fd[fh] = fd
        file_info = self.pyfuse3.FileInfo()
        file_info.fh = fh
        return file_info

    async def create(
        self,
        parent_inode: int,
        name: bytes,
        mode: int,
        flags: int,
        ctx: object | None = None,
    ) -> Any:
        parent = self._rel_for_inode(parent_inode)
        try:
            child_name = self.core.validate_name(name)
            rel = child_name if parent == "" else f"{parent}/{child_name}"
            if self.core._contains_reserved(rel):
                raise self.pyfuse3.FUSEError(errno.ENOENT)
            fd = os.open(self.core.source_path(rel), flags | os.O_CREAT, mode)
            self.core.ownership.apply(self.core.source_path(rel))
            entry = self.core.entry_for(rel)
        except ValueError:
            raise self.pyfuse3.FUSEError(errno.EINVAL) from None
        except OSError as exc:
            raise self.pyfuse3.FUSEError(exc.errno) from exc
        fh = self._next_fh
        self._next_fh += 1
        self._fh_to_fd[fh] = fd
        file_info = self.pyfuse3.FileInfo()
        file_info.fh = fh
        return file_info, self._attrs(entry)

    async def read(self, fh: int, off: int, size: int) -> bytes:
        fd = self._fh_to_fd.get(fh)
        if fd is None:
            raise self.pyfuse3.FUSEError(errno.EBADF)
        return os.pread(fd, size, off)

    async def write(self, fh: int, off: int, buf: bytes) -> int:
        fd = self._fh_to_fd.get(fh)
        if fd is None:
            raise self.pyfuse3.FUSEError(errno.EBADF)
        return os.pwrite(fd, buf, off)

    async def flush(self, fh: int) -> None:
        fd = self._fh_to_fd.get(fh)
        if fd is not None:
            os.fsync(fd)

    async def fsync(self, fh: int, datasync: bool) -> None:
        fd = self._fh_to_fd.get(fh)
        if fd is not None:
            os.fdatasync(fd) if datasync and hasattr(os, "fdatasync") else os.fsync(fd)

    async def release(self, fh: int) -> None:
        fd = self._fh_to_fd.pop(fh, None)
        if fd is not None:
            os.close(fd)

    async def unlink(self, parent_inode: int, name: bytes, ctx: object | None = None) -> None:
        parent = self._rel_for_inode(parent_inode)
        try:
            child_name = self.core.validate_name(name)
            rel = child_name if parent == "" else f"{parent}/{child_name}"
            if self.core._contains_reserved(rel):
                raise self.pyfuse3.FUSEError(errno.ENOENT)
            self.core.source_path(rel).unlink()
            self._drop_inode_for(rel)
        except ValueError:
            raise self.pyfuse3.FUSEError(errno.EINVAL) from None
        except OSError as exc:
            raise self.pyfuse3.FUSEError(exc.errno) from exc

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
    options.add("fsname=ccc-storage-observe")
    options.add("subtype=ccc-storage-observe")
    options.add("allow_other")
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
