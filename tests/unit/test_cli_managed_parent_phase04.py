from __future__ import annotations

from ccc_layered_cli.main import main as cli_main
from ccc_layered_mountd.control import ControlServer
from ccc_layered_mountd.daemon import MountdService


def test_cli_managed_parent_lifecycle_over_socket(fake_nfs, tmp_path, monkeypatch, capsys):
    service = MountdService(
        nfs_root=fake_nfs.ccc_layered,
        run_dir=tmp_path / "run",
        managed_parent="/managed/dataset",
    )
    sock = tmp_path / "mountd.sock"
    server = ControlServer(sock, service)
    server.start()
    monkeypatch.setenv("CCC_MOUNTD_SOCK", str(sock))
    try:
        assert cli_main(["create", "foo"]) == 0
        capsys.readouterr()

        assert cli_main(["parent-ls"]) == 0
        assert "foo" in capsys.readouterr().out

        assert cli_main(["rename", "foo", "bar"]) == 0
        capsys.readouterr()

        assert cli_main(["parent-ls"]) == 0
        out = capsys.readouterr().out
        assert "bar" in out and "foo" not in out

        assert cli_main(["rmdir", "bar"]) == 0
        capsys.readouterr()

        # Duplicate create surfaces a clear non-zero exit.
        assert cli_main(["create", "baz"]) == 0
        capsys.readouterr()
        assert cli_main(["create", "baz"]) == 2
        assert "already exists" in capsys.readouterr().out
    finally:
        server.stop()
