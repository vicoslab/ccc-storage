from __future__ import annotations

from ccc_storage_cli.main import main as cli_main
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, dump_atomic
from ccc_storage_mountd.control import ControlServer
from ccc_storage_mountd.daemon import MountdService


def test_cli_status_and_ls_roundtrip_over_control_socket(fake_nfs, tmp_path, monkeypatch, capsys):
    pack = fake_nfs.subdir("packs") / "foo.sqfs"
    pack.write_bytes(b"pack")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=3,
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    dump_atomic(fake_nfs.subdir("registry") / "foo.toml", manifest)

    service = MountdService(nfs_root=fake_nfs.ccc_storage, run_dir=tmp_path / "run")
    service.reload_registry()
    sock = tmp_path / "mountd.sock"
    server = ControlServer(sock, service)
    server.start()
    monkeypatch.setenv("CCC_MOUNTD_SOCK", str(sock))
    try:
        assert cli_main(["status", "dataset:foo", "--json"]) == 0
        assert '"generation": 3' in capsys.readouterr().out

        assert cli_main(["ls", "--json"]) == 0
        assert '"dataset:foo"' in capsys.readouterr().out
    finally:
        server.stop()


def test_cli_status_fails_fast_when_socket_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CCC_MOUNTD_SOCK", str(tmp_path / "missing.sock"))

    assert cli_main(["status", "dataset:foo"]) == 2
    assert "mountd not reachable" in capsys.readouterr().out.lower()
