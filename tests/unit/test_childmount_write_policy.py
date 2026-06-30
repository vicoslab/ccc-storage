from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ccc_layered_core.manifest import (
    WRITE_POLICY_LOCAL_SSD_ASYNC,
    WRITE_POLICY_SHARED_NFS,
    ChildManifest,
    OverlayInfo,
)
from ccc_layered_mountd import childmount
from ccc_layered_mountd.childmount import ChildMountManager
from ccc_layered_mountd.overlay import OverlayPaths


@dataclass
class FakeHandle:
    mountpoint: Path
    command: tuple[str, ...] = ("fake",)
    mounted: bool = True
    unmount_calls: int = 0

    def unmount(self):
        self.unmount_calls += 1
        self.mounted = False


def _manifest(tmp_path: Path, policy: str) -> ChildManifest:
    overlays = OverlayPaths.for_child(tmp_path / "nfs" / "overlays", "observe:env")
    return ChildManifest(
        id="observe:env",
        name="env",
        type="observed-child",
        generation=0,
        write_policy=policy,
        overlay=OverlayInfo(
            mode="shared-overlay",
            active_upper=str(overlays.active_upper),
            overlay_generation=0,
        ),
    )


def test_shared_nfs_policy_uses_existing_fuse_overlay_backend(monkeypatch, tmp_path):
    calls = []

    def fake_shared(packs, overlay_paths, mountpoint, **kwargs):
        calls.append((packs, overlay_paths, mountpoint, kwargs))
        return FakeHandle(Path(mountpoint))

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_shared)
    manager = ChildMountManager(
        tmp_path / "run",
        nfs_root=tmp_path / "nfs",
        local_overlay_root=tmp_path / "ssd",
    )

    record = manager.mount_rw(_manifest(tmp_path, WRITE_POLICY_SHARED_NFS))

    assert record.write_policy == WRITE_POLICY_SHARED_NFS
    assert len(calls) == 1


def test_local_ssd_async_policy_uses_kernel_overlay_backend(monkeypatch, tmp_path):
    calls = []

    def fake_kernel(packs, local_paths, mountpoint, **kwargs):
        calls.append((packs, local_paths, mountpoint, kwargs))
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
        return FakeHandle(Path(mountpoint), command=("mount", "-t", "overlay"))

    monkeypatch.setattr(childmount, "mount_layered_rw_kernel_overlay", fake_kernel)
    manager = ChildMountManager(
        tmp_path / "run",
        nfs_root=tmp_path / "nfs",
        local_overlay_root=tmp_path / "ssd",
        node_id="node-a",
    )

    record = manager.mount_rw(_manifest(tmp_path, WRITE_POLICY_LOCAL_SSD_ASYNC))

    assert record.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC
    assert record.mode == "rw"
    assert calls
    assert str(calls[0][1].active_upper).startswith(str(tmp_path / "ssd"))
    assert (tmp_path / "nfs" / "locks" / "observe%3Aenv.local-writer.lock").exists()
    manager.force_unmount("observe:env")
    assert not (tmp_path / "nfs" / "locks" / "observe%3Aenv.local-writer.lock").exists()
