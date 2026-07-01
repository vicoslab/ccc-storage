from __future__ import annotations

import json

from ccc_storage_cli import main as cli


def test_cli_compact_dispatches_dry_run_allow_base_json(monkeypatch, capsys):
    calls = []

    def fake_request(command, *, path="", payload=None):
        calls.append((command, path, payload))
        return 0, {
            "dry_run": True,
            "compaction": {
                "needed": True,
                "target_level": 2,
                "selected_packs": ["/p/a.sqfs", "/p/b.sqfs"],
                "blocked_reason": "",
            },
        }

    monkeypatch.setattr(cli, "_request", fake_request)

    assert cli.main(["compact", "dataset:foo", "--dry-run", "--allow-base", "--json"]) == 0

    assert calls == [
        (
            "compact",
            "dataset:foo",
            {"dry_run": True, "allow_base": True},
        )
    ]
    out = json.loads(capsys.readouterr().out)
    assert out["compaction"]["target_level"] == 2
