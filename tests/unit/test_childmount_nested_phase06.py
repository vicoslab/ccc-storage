from __future__ import annotations

from dataclasses import dataclass

from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_storage_mountd import childmount
from ccc_storage_mountd.childmount import ChildMountManager, NestedMountManager


@dataclass
class FakeHandle:
    mountpoint: object
    mounted: bool = True

    def unmount(self) -> None:
        self.mounted = False


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def _child(tmp_path, name: str) -> ChildManifest:
    pack = tmp_path / f"{name}.sqfs"
    pack.write_bytes(b"p")
    return ChildManifest(
        id=f"env:{name}",
        name=name,
        type="conda-env",
        generation=1,
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=1),)),
    )


def test_nested_access_mounts_only_accessed_child(monkeypatch, tmp_path):
    monkeypatch.setattr(childmount, "mount_stack_ro", lambda *a, **k: FakeHandle(mountpoint=a[1]))
    nested = NestedMountManager(ChildMountManager(tmp_path / "run"), parent_id="root")

    nested.access_child(_child(tmp_path, "env-a"))

    assert nested.active_child_count() == 1
    assert nested.is_child_mounted("env:env-a")
    assert not nested.is_child_mounted("env:env-b")


def test_idle_unmount_child_leaves_parent_mounted(monkeypatch, tmp_path):
    monkeypatch.setattr(childmount, "mount_stack_ro", lambda *a, **k: FakeHandle(mountpoint=a[1]))
    clock = FakeClock()
    mounts = ChildMountManager(tmp_path / "run", clock=clock)
    nested = NestedMountManager(mounts, parent_id="env:root")

    nested.mount_parent(_child(tmp_path, "root"))
    nested.access_child(_child(tmp_path, "env-a"))
    nested.release_child("env:env-a")

    clock.advance(100.0)
    reaped = nested.idle_reap(ttl=10.0)

    assert "env:env-a" in reaped
    assert nested.active_child_count() == 0
    assert nested.parent_mounted is True


def test_mount_count_ceiling_lazy_access_plus_idle_reaper(monkeypatch, tmp_path):
    monkeypatch.setattr(childmount, "mount_stack_ro", lambda *a, **k: FakeHandle(mountpoint=a[1]))
    clock = FakeClock()
    mounts = ChildMountManager(tmp_path / "run", clock=clock)
    nested = NestedMountManager(mounts, parent_id="root")
    children = [_child(tmp_path, f"env-{i}") for i in range(100)]

    # 100 boundaries defined, but lazy: nothing mounts until accessed.
    assert nested.active_child_count() == 0

    ttl = 10.0
    peak = 0
    for child in children:
        nested.access_child(child)
        peak = max(peak, nested.active_child_count())
        nested.release_child(child.id)
        clock.advance(ttl + 1.0)
        nested.idle_reap(ttl=ttl)

    # Without the reaper the active submount count would reach 100; lazy access
    # plus the idle reaper bounds it well under the boundary count.
    assert peak <= 2
    assert nested.active_child_count() == 0
