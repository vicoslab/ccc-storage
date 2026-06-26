from __future__ import annotations

from dataclasses import dataclass

from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_layered_mountd import childmount
from ccc_layered_mountd.childmount import ChildMountManager


@dataclass
class FakeHandle:
    mountpoint: object
    command: tuple[str, ...] = ("fake",)
    mounted: bool = True
    unmount_calls: int = 0

    def unmount(self) -> None:
        self.unmount_calls += 1
        self.mounted = False


def _manifest(tmp_path) -> ChildManifest:
    pack = tmp_path / "pack.sqfs"
    pack.write_bytes(b"pack")
    return ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )


def test_child_mount_manager_mounts_once_and_refcounts(monkeypatch, tmp_path):
    calls = []

    def fake_mount_ro(pack, mountpoint, prefer_kernel=False):
        calls.append((pack, mountpoint, prefer_kernel))
        return FakeHandle(mountpoint=mountpoint)

    monkeypatch.setattr(childmount, "mount_ro", fake_mount_ro)
    manager = ChildMountManager(tmp_path / "run")
    manifest = _manifest(tmp_path)

    first = manager.mount(manifest)
    second = manager.mount(manifest)

    assert first.mountpoint == second.mountpoint
    assert len(calls) == 1
    assert manager.status(manifest)["refcount"] == 2

    manager.unmount(manifest.id)
    assert manager.status(manifest)["mounted"] is True
    manager.unmount(manifest.id)
    assert manager.status(manifest)["mounted"] is False
    assert first.handle.unmount_calls == 1
