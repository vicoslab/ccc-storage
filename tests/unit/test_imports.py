"""Every package imports and exposes a ``__version__`` string (phase-00 AC)."""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "ccc_storage_core",
    "ccc_storage_pack",
    "ccc_storage_mountd",
    "ccc_storage_cli",
    "ccc_storage_hpc",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    mod = importlib.import_module(name)
    assert mod is not None


@pytest.mark.parametrize("name", PACKAGES)
def test_package_has_version_string(name: str) -> None:
    mod = importlib.import_module(name)
    assert isinstance(mod.__version__, str)
    assert mod.__version__  # non-empty
    # phase-00 scaffolding pins everything at 0.0.0; just assert dotted shape.
    parts = mod.__version__.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts)


def test_entrypoint_modules_import() -> None:
    """The four entry-point callables resolve and are callable."""
    from ccc_storage_cli.main import main as cli_main
    from ccc_storage_hpc.client import main as hpc_main
    from ccc_storage_mountd.daemon import main as mountd_main
    from ccc_storage_pack.cli import main as pack_main

    for fn in (pack_main, mountd_main, cli_main, hpc_main):
        assert callable(fn)
