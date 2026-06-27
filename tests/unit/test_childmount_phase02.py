from __future__ import annotations

from dataclasses import dataclass

import pytest

from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_layered_mountd import childmount
from ccc_layered_mountd.childmount import ChildMountError, ChildMountManager


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


def _stack_manifest(tmp_path) -> ChildManifest:
    base = tmp_path / "base.sqfs"
    delta = tmp_path / "delta.sqfs"
    base.write_bytes(b"base")
    delta.write_bytes(b"delta")
    return ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=2,
        pack_stack=PackStack(
            active_revision="g2",
            lowers=(
                PackInfo(path=str(base), sha256="b" * 64, size=4),
                PackInfo(path=str(delta), sha256="d" * 64, size=5),
            ),
        ),
    )


def test_child_mount_manager_mounts_once_and_refcounts(monkeypatch, tmp_path):
    calls = []

    def fake_mount_stack_ro(packs, mountpoint, prefer_kernel=False):
        calls.append((tuple(packs), mountpoint, prefer_kernel))
        return FakeHandle(mountpoint=mountpoint)

    monkeypatch.setattr(childmount, "mount_stack_ro", fake_mount_stack_ro)
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


def test_child_mount_manager_mounts_entire_pack_stack(monkeypatch, tmp_path):
    calls = []

    def fake_mount_stack_ro(packs, mountpoint, prefer_kernel=False):
        calls.append((tuple(packs), mountpoint, prefer_kernel))
        return FakeHandle(mountpoint=mountpoint)

    monkeypatch.setattr(childmount, "mount_stack_ro", fake_mount_stack_ro)
    manager = ChildMountManager(tmp_path / "run")
    manifest = _stack_manifest(tmp_path)

    manager.mount(manifest)

    assert len(calls) == 1
    mounted_packs = calls[0][0]
    assert [pack.path for pack in mounted_packs] == [
        manifest.pack_stack.lowers[0].path,
        manifest.pack_stack.lowers[1].path,
    ]


def test_mount_at_refuses_silent_reuse_at_wrong_mountpoint(monkeypatch, tmp_path):
    def fake_mount_stack_ro(packs, mountpoint, prefer_kernel=False):
        return FakeHandle(mountpoint=mountpoint)

    monkeypatch.setattr(childmount, "mount_stack_ro", fake_mount_stack_ro)
    manager = ChildMountManager(tmp_path / "run")
    manifest = _manifest(tmp_path)

    generic = manager.mount(manifest)
    nested_target = tmp_path / "run" / "mounts" / "parent" / "nested"
    nested_target.mkdir(parents=True)

    with pytest.raises(ChildMountError, match="already mounted"):
        manager.mount_at(manifest, nested_target)
    assert manager.status(manifest)["mountpoint"] == str(generic.mountpoint)
