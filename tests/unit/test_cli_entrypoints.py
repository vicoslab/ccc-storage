"""CLI entry-point stubs: clean exit, ``--version``, and stub-specific behavior.

Phase-00 CLIs do no mounting/packing/NFS mutation. We assert they exit 0, that
``--version`` prints the package version, and that the implemented offline bits
(``ccc-layered doctor``, ``ccc-layered-mountd --probe``) behave safely.
"""

from __future__ import annotations

import pytest

from ccc_layered_cli import __version__ as cli_version
from ccc_layered_cli.main import main as cli_main
from ccc_layered_hpc import __version__ as hpc_version
from ccc_layered_hpc.client import main as hpc_main
from ccc_layered_mountd import __version__ as mountd_version
from ccc_layered_mountd.daemon import main as mountd_main
from ccc_layered_pack import __version__ as pack_version
from ccc_layered_pack.cli import main as pack_main

# (main, version, prog-name) for the four entry points.
ENTRIES = [
    (pack_main, pack_version, "ccc-pack"),
    (mountd_main, mountd_version, "ccc-layered-mountd"),
    (cli_main, cli_version, "ccc-layered"),
    (hpc_main, hpc_version, "ccc-layered-hpc"),
]


@pytest.mark.parametrize("main, _version, prog", ENTRIES)
def test_no_args_exits_zero(main, _version, prog, capsys) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert out  # prints *something* (help / planned surface)


@pytest.mark.parametrize("main, version, prog", ENTRIES)
def test_version_flag(main, version, prog, capsys) -> None:
    # argparse `--version` raises SystemExit(0) after printing.
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert prog in out
    assert version in out


@pytest.mark.parametrize("main, _version, prog", ENTRIES)
def test_stub_announces_not_implemented(main, _version, prog, capsys) -> None:
    main([])
    out = capsys.readouterr().out.lower()
    # Every stub either announces "not yet implemented" or prints a help banner
    # naming itself; both are acceptable, neither does any real work.
    assert "not yet implemented" in out or prog in out


def test_pack_unknown_command_is_clean(capsys) -> None:
    assert pack_main(["build", "/nonexistent", "/tmp/out.sqfs"]) == 0
    assert "not yet implemented" in capsys.readouterr().out.lower()


def test_hpc_unknown_command_is_clean(capsys) -> None:
    assert hpc_main(["mount", "whatever"]) == 0
    assert "not yet implemented" in capsys.readouterr().out.lower()


def test_mountd_probe_is_lightweight_and_clean(capsys) -> None:
    assert mountd_main(["--probe"]) == 0
    out = capsys.readouterr().out
    assert "runtime probe" in out.lower()
    # Reports each runtime binary (present path or MISSING) — no mounting.
    for name in ("mksquashfs", "squashfuse", "fuse-overlayfs"):
        assert name in out


def test_cli_doctor_offline_exits_zero(capsys, monkeypatch) -> None:
    # No socket, no NFS root configured: doctor must still exit 0 (diagnostic,
    # not a gate) and report DOWN/unset rather than raising.
    monkeypatch.delenv("CCC_NFS_ROOT", raising=False)
    monkeypatch.setenv("CCC_MOUNTD_SOCK", "/nonexistent/ccc-mountd.sock")
    assert cli_main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "doctor" in out.lower()
    assert "mountd socket" in out.lower()


def test_cli_doctor_reports_nfs_root(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("CCC_NFS_ROOT", str(tmp_path))
    monkeypatch.setenv("CCC_MOUNTD_SOCK", "/nonexistent/ccc-mountd.sock")
    assert cli_main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path) in out


def test_cli_subcommand_stub_exits_zero(capsys) -> None:
    assert cli_main(["status"]) == 0
    assert "not yet implemented" in capsys.readouterr().out.lower()


def test_cli_no_subcommand_prints_help(capsys) -> None:
    assert cli_main([]) == 0
    assert "ccc-layered" in capsys.readouterr().out
