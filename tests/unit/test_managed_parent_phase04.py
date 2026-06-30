from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from ccc_layered_core.manifest import (
    ChildManifest,
    OverlayInfo,
    PackInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)
from ccc_layered_mountd import childmount
from ccc_layered_mountd.childmount import ChildMountManager
from ccc_layered_mountd.managed_parent import (
    ChildExistsError,
    ChildNotEmptyError,
    ChildNotFoundError,
    ManagedParent,
    ManagedParentError,
)
from ccc_layered_mountd.overlay import OverlayPaths


@dataclass
class FakeHandle:
    mountpoint: object
    command: tuple[str, ...] = ("fake",)
    mounted: bool = True

    def unmount(self) -> None:
        self.mounted = False


def _parent(fake_nfs, tmp_path, mounts=None) -> ManagedParent:
    return ManagedParent(
        fake_nfs.ccc_layered,
        tmp_path / "run",
        parent_path="/managed/dataset",
        mounts=mounts,
    )


def test_list_children_hides_markers_and_lists_manifest_children(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    mp.create_child("foo")
    mp.create_child("bar")

    # Internal junk dropped into the children dir must not show up in listings.
    (mp.children_dir / ".ccc-layered").write_text("marker")
    (mp.children_dir / ".foo.toml.tmp").write_text("half-written")

    assert mp.list_children() == ["bar", "foo"]


def test_create_child_is_atomic_and_initializes_overlay(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)

    status = mp.create_child("foo")
    assert status["name"] == "foo"
    assert status["generation"] == 0
    assert status["parent_path"] == "/managed/dataset"

    manifest = load_manifest(mp.manifest_path("foo"))
    assert manifest.parent_path == "/managed/dataset"
    assert manifest.generation == 0
    assert manifest.pack_stack.lowers == ()
    assert manifest.overlay.mode == "shared-overlay"

    upper = OverlayPaths.for_child(fake_nfs.ccc_layered / "overlays", manifest.id).active_upper
    assert upper.is_dir()

    with pytest.raises(ChildExistsError):
        mp.create_child("foo")


def test_create_child_rejects_unsafe_names(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    for bad in ("", ".", "..", "a/b", ".hidden"):
        with pytest.raises(ManagedParentError):
            mp.create_child(bad)


def test_concurrent_create_has_exactly_one_winner(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    n = 8
    barrier = threading.Barrier(n)
    wins: list[dict] = []
    losses: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            result = mp.create_child("race")
        except ManagedParentError as exc:
            with lock:
                losses.append(exc)
        else:
            with lock:
                wins.append(result)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1
    assert len(losses) == n - 1
    assert all(isinstance(exc, ChildExistsError) for exc in losses)
    assert mp.list_children() == ["race"]


def test_rename_updates_name_and_parent_path_atomically(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    mp.create_child("foo")

    status = mp.rename_child("foo", "baz")
    assert status["name"] == "baz"
    assert not mp.manifest_path("foo").exists()

    manifest = load_manifest(mp.manifest_path("baz"))
    assert manifest.name == "baz"
    assert manifest.parent_path == "/managed/dataset"
    assert mp.list_children() == ["baz"]

    mp.create_child("qux")
    with pytest.raises(ChildExistsError):
        mp.rename_child("baz", "qux")
    with pytest.raises(ChildNotFoundError):
        mp.rename_child("nope", "whatever")


def test_rmdir_removes_empty_gen0_child(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    mp.create_child("foo")
    manifest = load_manifest(mp.manifest_path("foo"))
    overlay_root = OverlayPaths.for_child(fake_nfs.ccc_layered / "overlays", manifest.id).root

    mp.remove_child("foo")
    assert not mp.manifest_path("foo").exists()
    assert not overlay_root.exists()
    assert mp.list_children() == []


def test_rmdir_refuses_committed_child_with_clear_error(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    pack = fake_nfs.subdir("packs") / "committed.sqfs"
    pack.write_bytes(b"pack")
    committed = ChildManifest(
        id="dataset:committed",
        name="committed",
        type="dataset",
        generation=2,
        parent_path="/managed/dataset",
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    dump_atomic(mp.manifest_path("committed"), committed)

    with pytest.raises(ManagedParentError) as excinfo:
        mp.remove_child("committed")
    assert "ccc-storage" in str(excinfo.value)
    assert mp.manifest_path("committed").exists()


def test_rmdir_refuses_nonempty_overlay(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    mp.create_child("dirty")
    manifest = load_manifest(mp.manifest_path("dirty"))
    upper = OverlayPaths.for_child(fake_nfs.ccc_layered / "overlays", manifest.id).active_upper
    (upper / "scratch.txt").write_text("dirty data")

    with pytest.raises(ChildNotEmptyError):
        mp.remove_child("dirty")
    assert mp.manifest_path("dirty").exists()


def test_rmdir_missing_child_raises_not_found(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    with pytest.raises(ChildNotFoundError):
        mp.remove_child("ghost")


def test_access_lazy_mounts_via_childmount_manager(fake_nfs, tmp_path, monkeypatch):
    monkeypatch.setattr(childmount, "mount_stack_ro", lambda *a, **k: FakeHandle(mountpoint=a[1]))
    mounts = ChildMountManager(tmp_path / "run")
    mp = _parent(fake_nfs, tmp_path, mounts=mounts)

    # A child with a real pack lower so the lazy mount has something to mount.
    pack = fake_nfs.subdir("packs") / "withpack.sqfs"
    pack.write_bytes(b"pack")
    manifest = ChildManifest(
        id="dataset:withpack",
        name="withpack",
        type="dataset",
        generation=1,
        parent_path="/managed/dataset",
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
        overlay=OverlayInfo(mode="shared-overlay"),
    )
    dump_atomic(mp.manifest_path("withpack"), manifest)

    status = mp.access_child("withpack")
    assert status["mounted"] is True
    assert status["refcount"] == 1

    # A second access reuses the mount and bumps the refcount.
    status2 = mp.access_child("withpack")
    assert status2["refcount"] == 2


def test_access_missing_child_raises_not_found(fake_nfs, tmp_path):
    mp = _parent(fake_nfs, tmp_path)
    with pytest.raises(ChildNotFoundError):
        mp.access_child("ghost")
