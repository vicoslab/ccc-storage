from __future__ import annotations

from ccc_storage_core.manifest import (
    ChildBoundary,
    ChildManifest,
    PackInfo,
    PackStack,
    dump_atomic,
)
from ccc_storage_mountd import childmount
from ccc_storage_mountd.daemon import MountdService
from ccc_storage_pack.builder import BOUNDARY_MARKER_NAME, pack_object_dir, safe_pack_name


def _pack_info(path, payload: bytes) -> PackInfo:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return PackInfo(path=str(path), sha256="a" * 64, size=len(payload))


def test_pack_object_dir_separates_root_and_nested_child_objects(fake_nfs):
    packs = fake_nfs.subdir("packs")

    root_dir = pack_object_dir(packs, "user-root:alice")
    child_dir = pack_object_dir(packs, "conda-env:alice:env-a")

    assert root_dir == packs / safe_pack_name("user-root:alice")
    assert child_dir == packs / safe_pack_name("conda-env:alice:env-a")
    assert child_dir != root_dir
    assert child_dir.parent == root_dir.parent


def test_mount_tree_mounts_nested_child_at_parent_boundary_path(monkeypatch, fake_nfs, tmp_path):
    packs = fake_nfs.subdir("packs")
    registry = fake_nfs.subdir("registry")
    root_pack = _pack_info(pack_object_dir(packs, "user-root:alice") / "base.sqfs", b"root")
    child_pack = _pack_info(
        pack_object_dir(packs, "conda-env:alice:env-a") / "base.sqfs",
        b"child",
    )
    parent = ChildManifest(
        id="user-root:alice",
        name="alice",
        type="user-root",
        generation=1,
        pack_stack=PackStack(active_revision="g1", lowers=(root_pack,)),
        child_boundaries=(ChildBoundary("conda/envs/env-a", "conda-env:alice:env-a"),),
    )
    child = ChildManifest(
        id="conda-env:alice:env-a",
        name="env-a",
        type="conda-env",
        generation=1,
        parent_id=parent.id,
        parent_path="conda/envs/env-a",
        pack_stack=PackStack(active_revision="g1", lowers=(child_pack,)),
    )
    dump_atomic(registry / "roots" / "alice.toml", parent)
    dump_atomic(registry / "envs" / "env-a.toml", child)

    calls = []

    class FakeHandle:
        def __init__(self, mountpoint):
            self.mountpoint = mountpoint
            self.command = ("fake",)
            self.mounted = True

        def unmount(self):
            self.mounted = False

    def fake_mount_stack_ro(packs_arg, mountpoint, prefer_kernel=False):
        calls.append((tuple(pack.path for pack in packs_arg), mountpoint, prefer_kernel))
        # A real parent pack contains this stub directory; create it in the fake
        # parent mountpoint so the child mount can be targeted there.
        if mountpoint.name == safe_pack_name("user-root:alice"):
            boundary = mountpoint / "conda" / "envs" / "env-a"
            boundary.mkdir(parents=True)
            (boundary / BOUNDARY_MARKER_NAME).write_text("")
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_stack_ro", fake_mount_stack_ro)
    service = MountdService(nfs_root=fake_nfs.ccc_storage, run_dir=tmp_path / "run")

    result = service.handle_mount_tree(parent.id)

    parent_mountpoint = tmp_path / "run" / "mounts" / safe_pack_name("user-root:alice")
    child_mountpoint = parent_mountpoint / "conda" / "envs" / "env-a"
    assert calls == [
        ((str(root_pack.path),), parent_mountpoint, False),
        ((str(child_pack.path),), child_mountpoint, False),
    ]
    assert result["id"] == parent.id
    assert result["mountpoint"] == str(parent_mountpoint)
    assert result["nested_mounts"] == [
        {
            "id": child.id,
            "path": "conda/envs/env-a",
            "mountpoint": str(child_mountpoint),
            "mounted": True,
        }
    ]
