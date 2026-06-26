"""Node harness — simulate N CCC "nodes" without real /storage.

A "node" is a separate process (its own future mountd instance), optionally in
its own mount namespace (``unshare -m``) so its mounts are private like a
separate host. All nodes point at the same fake-NFS dir; only node-local state
(sockets, mountpoints) lives under per-node ``run/`` dirs.

Phase-00 scope: start/stop a *placeholder* process per node and guarantee clean
teardown — terminate/reap every process and sweep for stray mounts under the
test root (FUSE-test safety rules). Real daemon wiring lands in phase-02.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# A harmless long-lived placeholder; replaced by a real mountd in phase-02.
_PLACEHOLDER_CMD = [sys.executable, "-c", "import time; time.sleep(3600)"]


def _mountinfo_points() -> list[str]:
    """Mount points visible to this process (field 5 of /proc/self/mountinfo)."""
    try:
        data = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return []
    points: list[str] = []
    for line in data.splitlines():
        # ... <mount-id> <parent-id> <maj:min> <root> <mount-point> ...
        parts = line.split(" ")
        if len(parts) > 4:
            points.append(parts[4])
    return points


def sweep_stray_mounts(under: str | Path) -> list[str]:
    """Force-unmount and return any mount points at/under *under*.

    Last-resort safety so a crashed test never leaves a stuck mount inside the
    workspace. Returns the list of mount points it acted on.
    """
    base = str(Path(under).resolve())
    fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
    swept: list[str] = []
    for mp in _mountinfo_points():
        try:
            rp = str(Path(mp).resolve())
        except OSError:
            continue
        if rp == base or rp.startswith(base + os.sep):
            if fusermount:
                subprocess.run([fusermount, "-u", "-z", mp], capture_output=True, check=False)
            subprocess.run(["umount", "-l", mp], capture_output=True, check=False)
            swept.append(mp)
    return swept


@dataclass
class Node:
    """A single simulated node (phase-00: a placeholder subprocess)."""

    name: str
    run_dir: Path
    nfs_root: Path
    use_mount_ns: bool = False
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        cmd = list(_PLACEHOLDER_CMD)
        if self.use_mount_ns:
            # -r maps the caller to root-in-namespace so -m (private mounts) is
            # permitted unprivileged; falls back to no-ns if unshare is absent.
            if shutil.which("unshare"):
                cmd = ["unshare", "-rm", *cmd]
        env = dict(os.environ)
        env["CCC_NFS_ROOT"] = str(self.nfs_root)
        env["CCC_NODE_RUN_DIR"] = str(self.run_dir)
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.run_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=5)
        self.proc = None


@contextlib.contextmanager
def node_cluster(
    n: int,
    nfs_root: str | Path,
    *,
    use_mount_ns: bool = False,
    base_run: str | Path | None = None,
) -> Iterator[list[Node]]:
    """Start *n* nodes on one shared fake-NFS; reap + sweep on exit."""
    test_root = Path(os.environ.get("CCC_TEST_ROOT", Path.cwd()))
    run_base = Path(base_run) if base_run is not None else test_root / "run"
    run_base.mkdir(parents=True, exist_ok=True)
    nodes: list[Node] = []
    try:
        for i in range(n):
            node = Node(
                name=f"node{i}",
                run_dir=run_base / f"node{i}",
                nfs_root=Path(nfs_root),
                use_mount_ns=use_mount_ns,
            )
            node.start()
            nodes.append(node)
        yield nodes
    finally:
        for node in nodes:
            node.stop()
        sweep_stray_mounts(run_base)
