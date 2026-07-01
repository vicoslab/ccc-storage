from __future__ import annotations

import hashlib

from ccc_storage_core.checksum import sha256_file


def test_sha256_file_streams_file_contents(tmp_path):
    path = tmp_path / "data.bin"
    payload = b"abc" * 1000
    path.write_bytes(payload)

    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()
