"""Fake-S3: works with moto when present, skips gracefully when it is absent."""

from __future__ import annotations

import pytest

from tests.fakes import fake_s3 as fake_s3_mod

_HAS_MOTO = fake_s3_mod.moto_available()


def test_moto_available_returns_bool() -> None:
    assert isinstance(fake_s3_mod.moto_available(), bool)


def test_fakes3_dataclass_defaults() -> None:
    info = fake_s3_mod.FakeS3(bucket="b")
    assert info.bucket == "b"
    assert info.endpoint_url is None  # moto in-process => no socket
    assert info.region == "us-east-1"
    assert info.access_key and info.secret_key


@pytest.mark.skipif(_HAS_MOTO, reason="moto present; testing the no-moto path")
def test_context_raises_without_moto() -> None:
    with pytest.raises(RuntimeError):
        with fake_s3_mod.fake_s3():
            pass


@pytest.mark.skipif(not _HAS_MOTO, reason="moto not installed; fake-S3 unavailable")
def test_context_creates_bucket_with_moto() -> None:
    import boto3

    with fake_s3_mod.fake_s3(bucket="ccc-unit") as info:
        assert info.bucket == "ccc-unit"
        client = boto3.client(
            "s3",
            region_name=info.region,
            aws_access_key_id=info.access_key,
            aws_secret_access_key=info.secret_key,
        )
        names = [b["Name"] for b in client.list_buckets()["Buckets"]]
        assert "ccc-unit" in names


def test_fake_s3_fixture_skips_or_yields(fake_s3) -> None:
    # If moto is missing the fixture itself skips (importorskip); if present we
    # receive a usable FakeS3. Either outcome is a pass for phase-00.
    assert fake_s3.bucket
