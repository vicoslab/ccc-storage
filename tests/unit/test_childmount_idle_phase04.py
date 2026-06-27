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


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


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


def test_idle_unmount_never_unmounts_while_refcount_positive(monkeypatch, tmp_path):
    monkeypatch.setattr(childmount, "mount_stack_ro", lambda *a, **k: FakeHandle(mountpoint=a[1]))
    clock = FakeClock()
    manager = ChildMountManager(tmp_path / "run", clock=clock)
    manifest = _manifest(tmp_path)

    manager.mount(manifest)
    clock.advance(10_000)

    # refcount is 1 -> idle reaper must leave it mounted no matter how old.
    assert manager.idle_unmount_expired(ttl=1.0) == []
    assert manager.status(manifest)["mounted"] is True


def test_idle_unmount_expires_only_after_ttl_at_refcount_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(childmount, "mount_stack_ro", lambda *a, **k: FakeHandle(mountpoint=a[1]))
    clock = FakeClock()
    manager = ChildMountManager(tmp_path / "run", clock=clock)
    manifest = _manifest(tmp_path)

    record = manager.mount(manifest)
    # Release the open handle: refcount drops to 0 but the mount lingers lazily.
    clock.advance(5.0)
    released = manager.release(manifest.id)
    assert released["refcount"] == 0
    assert manager.status(manifest)["mounted"] is True

    # Just released -> not yet idle.
    assert manager.idle_unmount_expired(ttl=10.0) == []
    assert manager.status(manifest)["mounted"] is True

    # Past the TTL -> reaped exactly once.
    clock.advance(10.0)
    assert manager.idle_unmount_expired(ttl=10.0) == [manifest.id]
    assert manager.status(manifest)["mounted"] is False
    assert record.handle.unmount_calls == 1

    # Idempotent: nothing left to reap.
    assert manager.idle_unmount_expired(ttl=10.0) == []


def test_release_refcount_floor_does_not_go_negative(monkeypatch, tmp_path):
    monkeypatch.setattr(childmount, "mount_stack_ro", lambda *a, **k: FakeHandle(mountpoint=a[1]))
    manager = ChildMountManager(tmp_path / "run")
    manifest = _manifest(tmp_path)

    manager.mount(manifest)
    assert manager.release(manifest.id)["refcount"] == 0
    assert manager.release(manifest.id)["refcount"] == 0
