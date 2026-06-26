from __future__ import annotations

import pytest

from ccc_layered_mountd import dispatcher_fuse


def test_mount_dispatcher_refuses_clearly_when_pyfuse3_missing(monkeypatch):
    def _raise() -> object:
        raise ImportError("no pyfuse3 here")

    monkeypatch.setattr(dispatcher_fuse, "_import_pyfuse3", _raise)

    assert dispatcher_fuse.available() is False
    with pytest.raises(dispatcher_fuse.DispatcherUnavailable) as excinfo:
        dispatcher_fuse.mount_dispatcher(object(), "/managed/dataset")
    assert "pyfuse3" in str(excinfo.value)


def test_mount_dispatcher_is_not_a_byte_path():
    # The adapter is a shallow namespace placeholder; it must never expose a
    # read/write byte path of its own (RK-7).
    assert not hasattr(dispatcher_fuse, "read")
    assert not hasattr(dispatcher_fuse, "write")
