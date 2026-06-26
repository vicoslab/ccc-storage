"""Read/mount/extract helpers for SquashFS packs."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


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
