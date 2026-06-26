"""Pack verification helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.manifest import PackInfo


class VerificationError(ValueError):
    """Raised when a pack does not match expected immutable metadata."""


@dataclass(frozen=True)
class VerificationResult:
    path: str
    sha256: str
    size: int
    file_count: int | None = None


def inspect_pack(path: str | Path, *, file_count: int | None = None) -> PackInfo:
    """Return metadata for a pack-like file by stat+checksum."""
    pack = Path(path)
    if not pack.is_file():
        raise FileNotFoundError(str(pack))
    return PackInfo(
        path=str(pack),
        sha256=sha256_file(pack),
        size=pack.stat().st_size,
        file_count=file_count,
    )


def verify_pack(
    path: str | Path,
    expected: PackInfo | None = None,
    *,
    sha256: str | None = None,
    size: int | None = None,
    file_count: int | None = None,
) -> VerificationResult:
    """Verify pack bytes against expected checksum/size metadata."""
    pack = Path(path)
    if not pack.is_file():
        raise VerificationError(f"pack does not exist: {pack}")

    actual_sha = sha256_file(pack)
    actual_size = pack.stat().st_size
    expected_sha = sha256 if sha256 is not None else (expected.sha256 if expected else None)
    expected_size = size if size is not None else (expected.size if expected else None)
    expected_count = (
        file_count if file_count is not None else (expected.file_count if expected else None)
    )

    if expected_sha and actual_sha != expected_sha:
        raise VerificationError(
            f"sha256 mismatch for {pack}: expected {expected_sha}, got {actual_sha}"
        )
    if expected_size is not None and actual_size != expected_size:
        raise VerificationError(
            f"size mismatch for {pack}: expected {expected_size}, got {actual_size}"
        )

    return VerificationResult(
        path=str(pack),
        sha256=actual_sha,
        size=actual_size,
        file_count=expected_count,
    )
