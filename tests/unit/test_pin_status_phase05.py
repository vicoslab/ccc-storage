from __future__ import annotations

from ccc_layered_cli.main import main as cli_main
from ccc_layered_core.manifest import (
    ChildManifest,
    PackInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)
from ccc_layered_core.protocol import Request
from ccc_layered_mountd.control import ControlServer
from ccc_layered_mountd.daemon import MountdService


def _write_child(fake_nfs, *, deltas=0, pinned=False):
    base = fake_nfs.subdir("packs") / "base.sqfs"
    base.write_bytes(b"base")
    lowers = [PackInfo(path=str(base), sha256="b" * 64, size=1000)]
    for i in range(deltas):
        lowers.append(PackInfo(path=f"/p/delta{i}.sqfs", sha256=str(i) * 64, size=1))
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=deltas,
        pinned=pinned,
        pack_stack=PackStack(active_revision=f"g{deltas}", lowers=tuple(lowers)),
    )
    manifest_path = fake_nfs.subdir("registry") / "foo.toml"
    dump_atomic(manifest_path, manifest)
    service = MountdService(nfs_root=fake_nfs.ccc_layered, run_dir=fake_nfs.root / "run")
    service.reload_registry()
    return service, manifest, manifest_path


def test_status_enrichment_includes_pin_delta_policy_and_compaction(fake_nfs):
    service, manifest, _ = _write_child(fake_nfs, deltas=2)
    status = service.handle_status(manifest.id)

    assert status["pinned"] is False
    assert status["delta_count"] == 2
    assert status["policy"]["mode"] == "auto"
    assert status["policy"]["decision"] in {"trigger", "manual", "noop"}
    assert "needed" in status["compaction"]


def test_handle_pin_persists_pinned_state(fake_nfs):
    service, manifest, manifest_path = _write_child(fake_nfs)

    status = service.handle_pin(manifest.id, pinned=True)
    assert status["pinned"] is True
    assert load_manifest(manifest_path).pinned is True

    status = service.handle_pin(manifest.id, pinned=False)
    assert status["pinned"] is False
    assert load_manifest(manifest_path).pinned is False


def test_dispatch_pin_command(fake_nfs):
    service, manifest, manifest_path = _write_child(fake_nfs)

    resp = service.dispatch(Request(command="pin", path=manifest.id, payload={"pinned": True}))
    assert resp.ok
    assert resp.result["pinned"] is True
    assert load_manifest(manifest_path).pinned is True


def test_cli_pin_and_clear_over_socket(fake_nfs, tmp_path, monkeypatch, capsys):
    service, manifest, manifest_path = _write_child(fake_nfs)
    sock = tmp_path / "mountd.sock"
    server = ControlServer(sock, service)
    server.start()
    monkeypatch.setenv("CCC_MOUNTD_SOCK", str(sock))
    try:
        assert cli_main(["pin", "dataset:foo", "--json"]) == 0
        assert load_manifest(manifest_path).pinned is True

        assert cli_main(["pin", "dataset:foo", "--clear", "--json"]) == 0
        assert load_manifest(manifest_path).pinned is False
    finally:
        server.stop()
