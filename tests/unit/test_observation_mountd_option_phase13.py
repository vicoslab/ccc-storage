from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_mountd_exposes_observation_mountpoint_option():
    daemon = (ROOT / "src" / "ccc_storage_mountd" / "daemon.py").read_text()
    config = (ROOT / "src" / "ccc_storage_mountd" / "config.py").read_text()
    combined = daemon + "\n" + config
    assert "--observe-mountpoint" in daemon
    assert "CCC_OBSERVE_MOUNTPOINT" in combined
    assert "observe_mountpoint" in combined
    assert "mount_observation_dispatcher" in daemon
