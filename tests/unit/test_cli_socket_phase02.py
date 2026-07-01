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


def test_cli_observe_init_creates_and_registers_observation_dir(tmp_path, monkeypatch, capsys):
    service = MountdService(nfs_root=tmp_path / "legacy-state", run_dir=tmp_path / "run")
    sock = tmp_path / "mountd.sock"
    server = ControlServer(sock, service)
    server.start()
    monkeypatch.setenv("CCC_MOUNTD_SOCK", str(sock))
    observation = tmp_path / "observations" / "envs"
    try:
        assert (
            cli_main(
                [
                    "observe",
                    "init",
                    str(observation),
                    "--state-subdir",
                    ".ccc-alt",
                    "--json",
                ]
            )
            == 0
        )
    finally:
        server.stop()

    assert '"state_subdir": ".ccc-alt"' in capsys.readouterr().out
    for name in ("registry", "packs", "overlays", "locks", "events"):
        assert (observation / ".ccc-alt" / name).is_dir()
    assert service.observation_router is not None
    assert (
        service.observation_router.resolve(str(observation / "child")).root.public_path
        == observation
    )
