"""Local object-store abstraction used by Phase 08 tests.

This intentionally avoids boto/S3 credentials. The interface is shaped like the
small subset the S3 mirror/recall code needs, but the implementation is just a
safe local directory rooted under a test temp path.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class ObjectStoreError(RuntimeError):
    """Raised when an object-store operation fails."""


class LocalObjectStore:
    """A deterministic, no-network object store backed by local files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        clean = key.strip("/")
        if not clean or ".." in Path(clean).parts:
            raise ObjectStoreError(f"unsafe object key: {key!r}")
        return self.root / clean

    def put_file(self, key: str, source: str | Path) -> None:
        src = Path(source)
        if not src.is_file():
            raise ObjectStoreError(f"source file does not exist: {src}")
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def put_bytes(self, key: str, data: bytes) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def get_file(self, key: str, dest: str | Path) -> None:
        src = self._path(key)
        if not src.is_file():
            raise ObjectStoreError(f"object not found: {key}")
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectStoreError(f"object not found: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
