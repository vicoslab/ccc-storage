from __future__ import annotations

import json
import time

import pytest

from ccc_layered_core.locks import LockHeld, LockInfo, NFSLock


def test_lockfile_acquire_writes_holder_metadata_and_release_removes_file(tmp_path):
    lock_path = tmp_path / "locks" / "pack.lock"
    lock = NFSLock(lock_path, op="commit").acquire()

    info = LockInfo.from_file(lock_path)
    assert info.op == "commit"
    assert info.pid > 0
    assert info.node

    lock.release()
    assert not lock_path.exists()


def test_lockfile_is_exclusive(tmp_path):
    lock_path = tmp_path / "pack.lock"
    first = NFSLock(lock_path).acquire()

    with pytest.raises(LockHeld):
        NFSLock(lock_path).acquire()

    first.release()


def test_stale_lock_can_be_stolen_conservatively(tmp_path):
    lock_path = tmp_path / "pack.lock"
    lock_path.write_text(
        json.dumps({"node": "old", "pid": 1, "op": "old", "acquired_ts": 1.0, "heartbeat_ts": 1.0})
    )

    lock = NFSLock(lock_path, stale_after=0.01).acquire(steal_stale=True)
    assert LockInfo.from_file(lock_path).op == "lock"
    lock.release()


def test_heartbeat_updates_lock_metadata(tmp_path):
    lock_path = tmp_path / "pack.lock"
    lock = NFSLock(lock_path).acquire()
    before = LockInfo.from_file(lock_path).heartbeat_ts
    time.sleep(0.01)

    lock.heartbeat()

    assert LockInfo.from_file(lock_path).heartbeat_ts > before
    lock.release()
