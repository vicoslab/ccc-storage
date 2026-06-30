from __future__ import annotations

from ccc_layered_cli import main as cli
from ccc_layered_core.manifest import WRITE_POLICY_LOCAL_SSD_ASYNC


def test_cli_write_policy_sends_policy_and_remount_payload(monkeypatch, capsys):
    captured = {}

    def fake_request(command, *, path="", payload=None):
        captured["command"] = command
        captured["path"] = path
        captured["payload"] = payload
        return 0, {"id": "observe:env", "write_policy": WRITE_POLICY_LOCAL_SSD_ASYNC}

    monkeypatch.setattr(cli, "_request", fake_request)

    code = cli.main(["write-policy", "observe:env", WRITE_POLICY_LOCAL_SSD_ASYNC, "--remount"])

    assert code == 0
    assert captured == {
        "command": "write-policy",
        "path": "observe:env",
        "payload": {"policy": WRITE_POLICY_LOCAL_SSD_ASYNC, "remount": True},
    }
    assert "write_policy" in capsys.readouterr().out
