"""CLI entry-point stubs: clean exit, ``--version``, and stub-specific behavior.

Phase-00 CLIs do no mounting/packing/NFS mutation. We assert they exit 0, that
``--version`` prints the package version, and that the implemented offline bits
(``ccc-storage doctor``, ``ccc-storage mountd --probe``) behave safely.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ccc_storage.main import main as storage_main
from ccc_storage_cli import __version__ as cli_version
from ccc_storage_cli.main import main as cli_main
from ccc_storage_hpc import __version__ as hpc_version
from ccc_storage_hpc.client import main as hpc_main
from ccc_storage_mountd import __version__ as mountd_version
from ccc_storage_mountd.daemon import main as mountd_main
from ccc_storage_pack import __version__ as pack_version
from ccc_storage_pack.cli import main as pack_main

ROOT = Path(__file__).resolve().parents[2]

# (main, version, prog-name) for the tool namespace implementations.
ENTRIES = [
    (pack_main, pack_version, "ccc-storage pack"),
    (mountd_main, mountd_version, "ccc-storage mountd"),
    (cli_main, cli_version, "ccc-storage"),
    (hpc_main, hpc_version, "ccc-storage hpc"),
]


def test_pyproject_exports_only_unified_console_script() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert data["project"]["scripts"] == {"ccc-storage": "ccc_storage.main:main"}


def test_storage_top_level_help_lists_tool_namespaces_and_direct_ops(capsys) -> None:
    assert storage_main([]) == 0
    out = capsys.readouterr().out
    for phrase in (
        "ccc-storage pack",
        "mountd",
        "hpc",
        "conda",
        "mamba",
        "benchmark",
        "doctor",
        "commit",
    ):
        assert phrase in out


def test_storage_dispatches_tool_namespace_version(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        storage_main(["pack", "--version"])
    assert ei.value.code == 0
    assert "ccc-storage pack" in capsys.readouterr().out


def test_storage_dispatches_direct_control_command(capsys, monkeypatch) -> None:
    monkeypatch.delenv("CCC_NFS_ROOT", raising=False)
    monkeypatch.setenv("CCC_MOUNTD_SOCK", "/nonexistent/ccc-mountd.sock")
    assert storage_main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "ccc-storage doctor" in out
    assert "mountd socket" in out.lower()


@pytest.mark.parametrize("main, _version, prog", ENTRIES)
def test_no_args_exits_zero(main, _version, prog, capsys) -> None:
    code = main([])
    if prog == "ccc-storage mountd":
        assert code == 2
    else:
        assert code == 0
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


def test_pack_build_missing_source_fails_cleanly(capsys) -> None:
    assert pack_main(["build", "/nonexistent", "/tmp/out.sqfs"]) == 2
    out = capsys.readouterr().out.lower()
    assert "source directory does not exist" in out


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


def test_cli_status_requires_path(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        cli_main(["status"])
    assert ei.value.code == 2


def test_cli_no_subcommand_prints_help(capsys) -> None:
    assert cli_main([]) == 0
    assert "ccc-storage" in capsys.readouterr().out
