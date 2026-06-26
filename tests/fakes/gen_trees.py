"""Synthetic tree generators — deterministic fixtures for packs/overlays.

All content is a pure function of its index/name, so two calls with the same
arguments produce byte-identical trees (required by phase-00 tests and by
reproducible pack checksums later). Trees are tiny (a few KB) and live under
``$CCC_TEST_ROOT``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _content(index: int, size: int) -> bytes:
    """Deterministic *size* bytes derived from *index* (no RNG, no clock)."""
    seed = hashlib.sha256(f"ccc-file-{index}".encode()).digest()
    if size <= 0:
        return b""
    reps = (size // len(seed)) + 1
    return (seed * reps)[:size]


def make_dataset(dest: str | Path, count: int, size: int = 4096, *, shard: int = 1000) -> Path:
    """Create *count* small files of *size* bytes, sharded into subdirs.

    Mimics the "millions of small files" workload at tiny scale. Deterministic:
    same (count, size, shard) -> identical bytes and layout.
    """
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        sub = root / f"shard-{i // shard:04d}"
        sub.mkdir(exist_ok=True)
        (sub / f"file-{i:06d}.bin").write_bytes(_content(i, size))
    return root


def make_conda_like_env(dest: str | Path) -> Path:
    """Create a conda-like env tree: bin/, lib/site-packages/, links, shebangs.

    Exercises the awkward bits packs must preserve: symlinks, hardlinks, and
    executable shebang scripts (RK-3). Hardlink creation is best-effort (some
    filesystems disallow it); the symlink and scripts are always present.
    """
    root = Path(dest)
    bindir = root / "bin"
    site = root / "lib" / "python3.11" / "site-packages"
    bindir.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)

    # A "real" interpreter binary + a symlink alias (python3 -> python3.11).
    (bindir / "python3.11").write_bytes(_content(0, 256))
    alias = bindir / "python3"
    if not alias.exists():
        os.symlink("python3.11", alias)

    # A shebang script (executable).
    script = bindir / "ccc-tool"
    script.write_text("#!/usr/bin/env python3\nprint('hello from ccc-tool')\n")
    script.chmod(0o755)

    # A package with a data file + a hardlink to it.
    pkg = site / "cccpkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("VALUE = 42\n")
    data = pkg / "data.bin"
    data.write_bytes(_content(7, 1024))
    hardlink = pkg / "data_hardlink.bin"
    if not hardlink.exists():
        try:
            os.link(data, hardlink)
        except OSError:
            # Filesystem without hardlink support: leave it absent.
            pass
    return root


def corrupt(path: str | Path, offset: int = 0, *, byte: int | None = None) -> None:
    """Corrupt one byte of *path* at *offset* (flip bits, or set to *byte*).

    Used to assert that ``verify_pack`` catches corruption before mount.
    """
    p = Path(path)
    data = bytearray(p.read_bytes())
    if offset >= len(data):
        data.extend(b"\x00" * (offset - len(data) + 1))
    data[offset] = (data[offset] ^ 0xFF) if byte is None else (byte & 0xFF)
    p.write_bytes(bytes(data))
