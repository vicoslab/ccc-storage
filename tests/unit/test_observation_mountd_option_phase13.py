from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_mountd_exposes_observation_mountpoint_option():
    daemon = (ROOT / "src" / "ccc_storage_mountd" / "daemon.py").read_text()
    assert "--observe-mountpoint" in daemon
    assert "CCC_OBSERVE_MOUNTPOINT" in daemon
    assert "mount_observation_dispatcher" in daemon
