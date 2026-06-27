"""Read/mount/extract helpers for SquashFS packs."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ccc_layered_core.manifest import PackInfo


class PackReadError(RuntimeError):
    """Raised when mounting or extracting a pack fails."""


@dataclass
class MountHandle:
    mountpoint: Path
    command: tuple[str, ...]
    mounted: bool = True

    def unmount(self) -> None:
        """Best-effort idempotent unmount."""
        if not self.mounted:
            return
        commands: list[list[str]] = []
        fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
        if fusermount:
            commands.append([fusermount, "-u", "-z", str(self.mountpoint)])
        if shutil.which("umount"):
            commands.append(["umount", "-l", str(self.mountpoint)])
        for cmd in commands:
            subprocess.run(cmd, capture_output=True, check=False)
        self.mounted = False


@dataclass
class StackMountHandle(MountHandle):
    lower_handles: tuple[MountHandle, ...] = ()
    stack_root: Path | None = None

    def unmount(self) -> None:
        super().unmount()
        for handle in reversed(self.lower_handles):
            handle.unmount()
        if self.stack_root is not None:
            shutil.rmtree(self.stack_root, ignore_errors=True)


def mount_ro(
    pack: str | Path,
    mountpoint: str | Path,
    *,
    prefer_kernel: bool = False,
) -> MountHandle:
    """Mount a pack read-only at caller-provided *mountpoint*."""
    pack_path = Path(pack)
    mnt = Path(mountpoint)
    mnt.mkdir(parents=True, exist_ok=True)

    if prefer_kernel and shutil.which("mount"):
        cmd = ["mount", "-t", "squashfs", "-o", "loop,ro", str(pack_path), str(mnt)]
    else:
        squashfuse = shutil.which("squashfuse")
        if not squashfuse:
            raise PackReadError("squashfuse not found; cannot mount pack unprivileged")
        cmd = [squashfuse, str(pack_path), str(mnt)]

    cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        msg = cp.stderr.strip() or cp.stdout.strip()
        raise PackReadError(f"mount failed ({cp.returncode}): {msg}")
    return MountHandle(mountpoint=mnt, command=tuple(cmd))


def mount_stack_ro(
    packs: tuple[PackInfo, ...] | list[PackInfo],
    mountpoint: str | Path,
    *,
    prefer_kernel: bool = False,
) -> MountHandle:
    """Mount a committed pack stack as one read-only view.

    ``PackStack.lowers`` is stored base-first, delta-last. Overlay lowerdir order
    is top-first, so the mounted lower directories are passed to fuse-overlayfs
    in reverse order: latest delta first, base last.
    """
    if not packs:
        raise PackReadError("cannot mount an empty pack stack")
    if len(packs) == 1:
        return mount_ro(packs[0].path, mountpoint, prefer_kernel=prefer_kernel)

    fuse_overlayfs = shutil.which("fuse-overlayfs")
    if not fuse_overlayfs:
        raise PackReadError("fuse-overlayfs not found; cannot compose pack stack")

    mnt = Path(mountpoint)
    mnt.mkdir(parents=True, exist_ok=True)
    stack_root = mnt.parent / f".{mnt.name}.stack"
    if stack_root.exists():
        shutil.rmtree(stack_root)
    lowers_root = stack_root / "lowers"
    lowers_root.mkdir(parents=True, exist_ok=True)

    lower_handles: list[MountHandle] = []
    try:
        for idx, pack in enumerate(packs):
            lower_mnt = lowers_root / f"{idx:04d}"
            lower_handles.append(
                mount_ro(pack.path, lower_mnt, prefer_kernel=prefer_kernel)
            )
        lowerdirs = ":".join(str(handle.mountpoint) for handle in reversed(lower_handles))
        cmd = [fuse_overlayfs, "-o", f"lowerdir={lowerdirs}", str(mnt)]
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if cp.returncode != 0:
            msg = cp.stderr.strip() or cp.stdout.strip()
            raise PackReadError(f"stack mount failed ({cp.returncode}): {msg}")
        return StackMountHandle(
            mountpoint=mnt,
            command=tuple(cmd),
            lower_handles=tuple(lower_handles),
            stack_root=stack_root,
        )
    except Exception:
        for handle in reversed(lower_handles):
            handle.unmount()
        shutil.rmtree(stack_root, ignore_errors=True)
        raise


def extract(pack: str | Path, dest: str | Path, *, subpath: str | None = None) -> None:
    """Extract *pack* into *dest* using unsquashfs."""
    unsquashfs = shutil.which("unsquashfs")
    if not unsquashfs:
        raise PackReadError("unsquashfs not found; install squashfs-tools")
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    cmd = [unsquashfs, "-f", "-d", str(dest_path), str(pack)]
    if subpath:
        cmd.append(subpath)
    cp = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        msg = cp.stderr.strip() or cp.stdout.strip()
        raise PackReadError(f"unsquashfs failed ({cp.returncode}): {msg}")
