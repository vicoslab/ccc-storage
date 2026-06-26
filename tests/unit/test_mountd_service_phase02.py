from __future__ import annotations

from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack, dump_atomic
from ccc_layered_mountd import childmount
from ccc_layered_mountd.daemon import MountdService


def _write_manifest(registry, child_id="dataset:foo", name="foo") -> ChildManifest:
    pack = registry.parent / "packs" / "foo.sqfs"
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_bytes(b"pack")
    manifest = ChildManifest(
        id=child_id,
        name=name,
        type="dataset",
        generation=7,
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    path = registry / "datasets" / "foo.toml"
    dump_atomic(path, manifest)
    return manifest


def test_mountd_service_scans_registry_and_reports_status(fake_nfs, tmp_path):
    manifest = _write_manifest(fake_nfs.subdir("registry"))
    service = MountdService(nfs_root=fake_nfs.ccc_layered, run_dir=tmp_path / "run")

    service.reload_registry()
    listed = service.handle_ls()["children"]
    status = service.handle_status(manifest.id)

    assert listed[0]["id"] == manifest.id
    assert status["id"] == manifest.id
    assert status["generation"] == 7
    assert status["mounted"] is False


def test_mountd_service_mount_and_umount_delegate_to_childmount(monkeypatch, fake_nfs, tmp_path):
    manifest = _write_manifest(fake_nfs.subdir("registry"))
    calls = []

    class FakeHandle:
        def __init__(self, mountpoint):
            self.mountpoint = mountpoint
            self.command = ("fake",)
            self.mounted = True

        def unmount(self):
            calls.append("unmount")
            self.mounted = False

    def fake_mount_ro(pack, mountpoint, prefer_kernel=False):
        calls.append((pack, mountpoint))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_ro", fake_mount_ro)
    service = MountdService(nfs_root=fake_nfs.ccc_layered, run_dir=tmp_path / "run")
    service.reload_registry()

    mounted = service.handle_mount(manifest.id)
    assert mounted["mounted"] is True
    assert calls and calls[0][0] == manifest.pack_stack.lowers[0].path

    unmounted = service.handle_umount(manifest.id)
    assert unmounted["mounted"] is False
    assert "unmount" in calls
