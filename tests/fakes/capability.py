"""Capability probe — the single source of truth for "what can this host do?".

Computed once per session (``CAPS``) via **active** probes: each capability is
verified by actually attempting a tiny mount/namespace operation in a temp dir
under ``$CCC_TEST_ROOT``, wrapped in a short timeout. A missing binary
short-circuits to ``False`` without spawning anything, so the probe is fast and
**never hangs**. Anything that times out or errors fails *closed* (reported
``False``) — capabilities are never assumed.

See ``implementation-planning/testing/fuse-runtime-tests.md``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Per-probe timeout (seconds). Kept short so a broken FUSE on some host fails
# the capability closed instead of blocking the suite.
PROBE_TIMEOUT = float(os.environ.get("CCC_PROBE_TIMEOUT", "5"))


def _test_root() -> Path:
    """Resolve a scratch dir for probe mounts (env, else repo/.scratch)."""
    value = os.environ.get("CCC_TEST_ROOT")
    root = Path(value) if value else Path(__file__).resolve().parents[2] / ".scratch"
    root = root / "cap-probe"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes] | None:
    """Run *cmd* with a timeout; return None on timeout / missing exe / OS error."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _ok(cmd: list[str]) -> bool:
    cp = _run(cmd)
    return cp is not None and cp.returncode == 0


def _fusermount_exe() -> str | None:
    return shutil.which("fusermount3") or shutil.which("fusermount")


def _unmount(mountpoint: Path) -> None:
    exe = _fusermount_exe()
    if exe:
        _run([exe, "-u", "-z", str(mountpoint)])
    _run(["umount", "-l", str(mountpoint)])


# --- individual probes -------------------------------------------------------


def _probe_dev_fuse() -> bool:
    return os.path.exists("/dev/fuse") and os.access("/dev/fuse", os.R_OK | os.W_OK)


def _probe_fusermount() -> bool:
    exe = _fusermount_exe()
    if not exe:
        return False
    # `fusermount3 --version` may exit non-zero on some builds; presence + a
    # successful exec (no exception) is the real signal here.
    return _run([exe, "--version"]) is not None


def _probe_userns() -> bool:
    if not shutil.which("unshare"):
        return False
    return _ok(["unshare", "-r", "true"])


def _probe_mountns() -> bool:
    if not shutil.which("unshare"):
        return False
    return _ok(["unshare", "-rm", "true"])


def _make_tiny_pack(workdir: Path) -> Path | None:
    """Build a 1-file .sqfs with mksquashfs; None if the tool is unavailable."""
    mksquashfs = shutil.which("mksquashfs")
    if not mksquashfs:
        return None
    src = workdir / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "hello.txt").write_text("hi\n")
    pack = workdir / "tiny.sqfs"
    if not _ok([mksquashfs, str(src), str(pack), "-noappend", "-no-progress"]):
        return None
    return pack if pack.is_file() else None


def _probe_unpriv_fuse(root: Path) -> bool:
    sqf = shutil.which("squashfuse")
    if not sqf or not _fusermount_exe() or not _probe_dev_fuse():
        return False
    work = Path(tempfile.mkdtemp(dir=root, prefix="unpriv-fuse-"))
    mnt = work / "mnt"
    try:
        pack = _make_tiny_pack(work)
        if pack is None:
            return False
        mnt.mkdir()
        if not _ok([sqf, str(pack), str(mnt)]):
            return False
        return (mnt / "hello.txt").is_file()
    finally:
        _unmount(mnt)
        shutil.rmtree(work, ignore_errors=True)


def _probe_fuse_overlayfs(root: Path) -> bool:
    exe = shutil.which("fuse-overlayfs")
    if not exe or not _fusermount_exe() or not _probe_dev_fuse():
        return False
    work = Path(tempfile.mkdtemp(dir=root, prefix="fuse-ovl-"))
    lower, upper, wk, merged = (work / d for d in ("lower", "upper", "work", "merged"))
    try:
        for d in (lower, upper, wk, merged):
            d.mkdir()
        (lower / "base.txt").write_text("base\n")
        opt = f"lowerdir={lower},upperdir={upper},workdir={wk}"
        if not _ok([exe, "-o", opt, str(merged)]):
            return False
        return (merged / "base.txt").is_file()
    finally:
        _unmount(merged)
        shutil.rmtree(work, ignore_errors=True)


def _probe_kernel_squashfs(root: Path, mountns: bool) -> bool:
    if not mountns:
        return False
    work = Path(tempfile.mkdtemp(dir=root, prefix="kern-sqfs-"))
    mnt = work / "mnt"
    try:
        pack = _make_tiny_pack(work)
        if pack is None:
            return False
        mnt.mkdir()
        script = f"mount -t squashfs -o loop {pack} {mnt} && umount {mnt}"
        return _ok(["unshare", "-rm", "sh", "-c", script])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _probe_kernel_overlay(root: Path, mountns: bool) -> bool:
    if not mountns:
        return False
    work = Path(tempfile.mkdtemp(dir=root, prefix="kern-ovl-"))
    lower, upper, wk, merged = (work / d for d in ("lower", "upper", "work", "merged"))
    try:
        for d in (lower, upper, wk, merged):
            d.mkdir()
        opt = f"lowerdir={lower},upperdir={upper},workdir={wk}"
        script = f"mount -t overlay overlay -o {opt} {merged} && umount {merged}"
        return _ok(["unshare", "-rm", "sh", "-c", script])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _probe_docker() -> bool:
    if not shutil.which("docker"):
        return False
    return _ok(["docker", "info"])


# --- the struct + the cached result ------------------------------------------


@dataclass(frozen=True)
class Caps:
    dev_fuse: bool
    fusermount: bool
    unpriv_fuse: bool
    userns: bool
    mountns: bool
    kernel_squashfs: bool
    kernel_overlay: bool
    fuse_overlayfs: bool
    docker: bool


def probe() -> Caps:
    """Run every active probe once and return the result."""
    root = _test_root()
    mountns = _probe_mountns()
    return Caps(
        dev_fuse=_probe_dev_fuse(),
        fusermount=_probe_fusermount(),
        unpriv_fuse=_probe_unpriv_fuse(root),
        userns=_probe_userns(),
        mountns=mountns,
        kernel_squashfs=_probe_kernel_squashfs(root, mountns),
        kernel_overlay=_probe_kernel_overlay(root, mountns),
        fuse_overlayfs=_probe_fuse_overlayfs(root),
        docker=_probe_docker(),
    )


# Session-cached: computed once when this module is first imported.
CAPS: Caps = probe()


# --- pytest skip markers -----------------------------------------------------
# Guarded so the module is importable (e.g. via `python -c`) without pytest.
try:
    import pytest

    require_dev_fuse = pytest.mark.skipif(not CAPS.dev_fuse, reason="no /dev/fuse")
    require_unpriv_fuse = pytest.mark.skipif(
        not CAPS.unpriv_fuse, reason="no unprivileged FUSE on this host"
    )
    require_kernel_mount = pytest.mark.skipif(
        not (CAPS.userns and CAPS.mountns), reason="no user/mount namespaces"
    )
    require_fuse_overlay = pytest.mark.skipif(
        not CAPS.fuse_overlayfs, reason="fuse-overlayfs unavailable"
    )
    require_docker = pytest.mark.skipif(not CAPS.docker, reason="no Docker daemon")
except ImportError:  # pragma: no cover - pytest is always present in tests
    require_dev_fuse = None
    require_unpriv_fuse = None
    require_kernel_mount = None
    require_fuse_overlay = None
    require_docker = None
