from __future__ import annotations

import json

from ccc_storage_cli import main as cli


def test_cli_cold_status_archive_recall_dispatch(monkeypatch, capsys):
    calls = []

    def fake_request(command, *, path="", payload=None):
        calls.append((command, path, payload))
        return 0, {"command": command, "path": path, "payload": payload or {}}

    monkeypatch.setattr(cli, "_request", fake_request)

    assert cli.main(["cold", "status", "dataset:foo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "cold-status"

    assert cli.main(["cold", "archive", "dataset:foo", "--keep-hot", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["payload"] == {"keep_hot": True}

    assert cli.main(["cold", "recall", "dataset:foo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "cold-recall"

    assert calls == [
        ("cold-status", "dataset:foo", {}),
        ("cold-archive", "dataset:foo", {"keep_hot": True}),
        ("cold-recall", "dataset:foo", {}),
    ]
