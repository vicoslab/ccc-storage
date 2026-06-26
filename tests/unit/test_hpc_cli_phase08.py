from __future__ import annotations

from ccc_layered_hpc.client import main


def test_hpc_client_status_stub_is_explicit(capsys):
    assert main(["status", "job-a"]) == 0
    out = capsys.readouterr().out
    assert "job-a" in out
    assert "staged" in out
