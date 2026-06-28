from __future__ import annotations

from dataclasses import replace

from ccc_layered_core.manifest import PackInfo, PackStack, dump_atomic, load_manifest
from ccc_layered_core.observe import OBSERVE_MARKER_NAME
from ccc_layered_mountd import childmount
from ccc_layered_mountd.daemon import MountdService


class FakeHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True

    def unmount(self):
        self.mounted = False


def test_observe_mkdir_registers_child_manifest_without_mounting(fake_nfs, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")

    service = MountdService(
        nfs_root=fake_nfs.ccc_layered,
        run_dir=tmp_path / "run",
        observe_root=source,
    )

    status = service.handle_observe_mkdir("user1")

    assert status["id"] == "observe:user1"
    assert status["parent_path"] == "user1"
    assert status["mounted"] is False
    assert (source / "user1").is_dir()
    manifest = load_manifest(fake_nfs.ccc_layered / "registry" / "observe" / "user1.toml")
    assert manifest.id == "observe:user1"
    assert manifest.pack_stack.lowers == ()
    assert service.mounts.active_count() == 0


def test_observe_ls_reports_discovered_and_registered_children(fake_nfs, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    (source / "user1").mkdir()

    service = MountdService(
        nfs_root=fake_nfs.ccc_layered,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    before = service.handle_observe_ls()["children"]
    assert before == [
        {
            "id": "observe:user1",
            "path": "user1",
            "registered": False,
            "generation": 0,
            "mounted": False,
            "status": None,
        }
    ]

    service.handle_observe_mkdir("user1")
    after = service.handle_observe_ls()["children"]
    assert after[0]["registered"] is True
    assert after[0]["generation"] == 0
    assert after[0]["mounted"] is False
    assert after[0]["status"]["id"] == "observe:user1"


def test_observe_access_mounts_only_requested_child_and_nested_roots_work(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    calls = []

    def fake_mount_stack_ro(packs, mountpoint, prefer_kernel=False):
        calls.append((tuple(pack.path for pack in packs), mountpoint))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_stack_ro", fake_mount_stack_ro)
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    nested = source / "user1" / "conda"
    nested.mkdir(parents=True)
    (nested / OBSERVE_MARKER_NAME).write_text("")

    service = MountdService(
        nfs_root=fake_nfs.ccc_layered,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    user1 = service.handle_observe_mkdir("user1")
    user2 = service.handle_observe_mkdir("user2")
    env_a = service.handle_observe_mkdir("user1/conda/env-a")

    for status in (user1, user2, env_a):
        manifest_path = (
            fake_nfs.ccc_layered / "registry" / "observe" / f"{status['safe_name']}.toml"
        )
        manifest = load_manifest(manifest_path)
        pack_path = fake_nfs.ccc_layered / "packs" / status["safe_name"] / "base.sqfs"
        pack_path.parent.mkdir(parents=True, exist_ok=True)
        pack_path.write_bytes(status["id"].encode())
        updated = replace(
            manifest,
            generation=1,
            pack_stack=PackStack(
                active_revision="g1",
                lowers=(
                    PackInfo(
                        path=str(pack_path),
                        sha256="a" * 64,
                        size=pack_path.stat().st_size,
                    ),
                ),
            ),
        )
        dump_atomic(manifest_path, updated)

    assert service.mounts.active_count() == 0

    mounted = service.handle_observe_access("user1/conda/env-a/bin/python")

    assert mounted["id"] == "observe:user1/conda/env-a"
    assert mounted["mounted"] is True
    assert service.mounts.active_ids() == ["observe:user1/conda/env-a"]
    assert len(calls) == 1
