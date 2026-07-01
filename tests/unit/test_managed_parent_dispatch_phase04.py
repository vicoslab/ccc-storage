from __future__ import annotations

from ccc_storage_core.protocol import Request
from ccc_storage_mountd.daemon import MountdService


def _service(fake_nfs, tmp_path) -> MountdService:
    return MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        managed_parent="/managed/dataset",
    )


def test_dispatch_managed_parent_namespace_ops(fake_nfs, tmp_path):
    service = _service(fake_nfs, tmp_path)

    created = service.dispatch(Request(command="create", path="foo"))
    assert created.ok
    assert created.result["name"] == "foo"

    listed = service.dispatch(Request(command="parent-ls"))
    assert listed.ok
    assert listed.result["children"] == ["foo"]

    renamed = service.dispatch(Request(command="rename", path="foo", payload={"to": "bar"}))
    assert renamed.ok
    assert renamed.result["name"] == "bar"

    removed = service.dispatch(Request(command="rmdir", path="bar"))
    assert removed.ok
    assert removed.result["removed"] is True

    again = service.dispatch(Request(command="parent-ls"))
    assert again.result["children"] == []


def test_dispatch_duplicate_create_returns_eexist(fake_nfs, tmp_path):
    service = _service(fake_nfs, tmp_path)
    assert service.dispatch(Request(command="create", path="foo")).ok

    dup = service.dispatch(Request(command="create", path="foo"))
    assert dup.ok is False
    assert dup.code == "EEXIST"


def test_dispatch_managed_parent_commands_require_configured_parent(fake_nfs, tmp_path):
    service = MountdService(nfs_root=fake_nfs.ccc_storage, run_dir=tmp_path / "run")
    resp = service.dispatch(Request(command="create", path="foo"))
    assert resp.ok is False
    assert resp.code == "EPROTO"
