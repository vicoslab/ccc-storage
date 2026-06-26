"""Fake-S3 — an in-process S3 with no credentials and no network.

``moto`` is the default backend (in-process; nothing listens on a socket). A
``minio`` subprocess is an optional alternative for tests that need a real
endpoint URL; it is only used when the ``minio`` binary is present. If neither
is available the caller should skip (``pytest.importorskip("moto")`` in the
fixture handles the common case).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass
class FakeS3:
    bucket: str
    endpoint_url: str | None = None  # None => moto in-process (no socket)
    region: str = "us-east-1"
    access_key: str = "testing"
    secret_key: str = "testing"


def moto_available() -> bool:
    try:
        import moto  # noqa: F401
    except ImportError:
        return False
    return True


@contextlib.contextmanager
def fake_s3(bucket: str = "ccc-test") -> Iterator[FakeS3]:
    """Yield a :class:`FakeS3` backed by moto; create the bucket up front.

    Raises ``RuntimeError`` if moto/boto3 are unavailable — callers that want a
    skip should guard with ``pytest.importorskip("moto")``.
    """
    try:
        import boto3
        import moto
    except ImportError as exc:  # pragma: no cover - exercised only without moto
        raise RuntimeError("moto/boto3 not installed; fake-S3 unavailable") from exc

    # moto >= 5 exposes mock_aws; older releases used mock_s3.
    mock = getattr(moto, "mock_aws", None) or moto.mock_s3
    with mock():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket=bucket)
        yield FakeS3(bucket=bucket)
