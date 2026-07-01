from __future__ import annotations

import pytest

from ccc_storage_pack.verify import VerificationError, inspect_pack, verify_pack


def test_inspect_and_verify_pack_bytes(tmp_path):
    pack = tmp_path / "p.sqfs"
    pack.write_bytes(b"fake immutable pack bytes")

    info = inspect_pack(pack, file_count=3)
    result = verify_pack(pack, info)

    assert info.size == pack.stat().st_size
    assert info.file_count == 3
    assert result.sha256 == info.sha256


def test_verify_detects_corruption(tmp_path):
    pack = tmp_path / "p.sqfs"
    pack.write_bytes(b"abc")
    info = inspect_pack(pack)
    pack.write_bytes(b"abd")

    with pytest.raises(VerificationError):
        verify_pack(pack, info)


def test_verify_detects_truncation(tmp_path):
    pack = tmp_path / "p.sqfs"
    pack.write_bytes(b"abcdef")
    info = inspect_pack(pack)
    pack.write_bytes(b"abc")

    with pytest.raises(VerificationError):
        verify_pack(pack, sha256=info.sha256, size=info.size)
