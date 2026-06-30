from __future__ import annotations

import pytest

from ccc_layered_cli.main import main as layered_main
from ccc_layered_hpc.client import main as hpc_main
from ccc_layered_mountd.daemon import main as mountd_main
from ccc_layered_pack.cli import main as pack_main
from ccc_storage.main import main as storage_main


@pytest.mark.parametrize(
    "main,argv,expected",
    [
        (storage_main, ["--version"], "ccc-storage"),
        (pack_main, ["--version"], "ccc-storage pack"),
        (mountd_main, ["--version"], "ccc-storage mountd"),
        (layered_main, ["--version"], "ccc-storage"),
        (hpc_main, ["--version"], "ccc-storage hpc"),
    ],
)
def test_every_entrypoint_reports_version(main, argv, expected, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 0
    assert expected in capsys.readouterr().out
