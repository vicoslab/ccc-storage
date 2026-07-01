from __future__ import annotations

import pytest

from ccc_storage_hpc.object_store import Boto3ObjectStore, ObjectStoreError


class FakeClientError(Exception):
    def __init__(self, code: str, message: str = "fake") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()
        self.deleted: list[tuple[str, str]] = []

    def create_bucket(self, Bucket):  # noqa: N803
        self.buckets.add(Bucket)
        return {}

    def head_bucket(self, Bucket):  # noqa: N803
        if Bucket not in self.buckets:
            raise FakeClientError("404")
        return {}

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else Body
        self.objects[(Bucket, Key)] = data
        self.buckets.add(Bucket)
        return {"ETag": "fake"}

    def get_object(self, Bucket, Key):  # noqa: N803
        try:
            return {"Body": FakeBody(self.objects[(Bucket, Key)])}
        except KeyError as exc:
            raise FakeClientError("NoSuchKey") from exc

    def head_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise FakeClientError("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        keys = sorted(
            key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)
        )
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        for item in Delete["Objects"]:
            key = item["Key"]
            self.objects.pop((Bucket, key), None)
            self.deleted.append((Bucket, key))
        return {}


@pytest.fixture
def fake_store() -> tuple[Boto3ObjectStore, FakeS3Client]:
    client = FakeS3Client()
    store = Boto3ObjectStore(
        bucket="ccc-test",
        endpoint_url="https://ceph-7.fri.uni-lj.si",
        addressing_style="auto",
        client=client,
    )
    assert store.config.request_checksum_calculation == "when_required"
    assert store.config.response_checksum_validation == "when_required"
    return store, client


def test_boto3_object_store_put_get_exists_and_read_bytes(fake_store, tmp_path):
    store, client = fake_store
    src = tmp_path / "pack.sqfs"
    src.write_bytes(b"pack-bytes")

    store.put_file("prefix/packs/pack.sqfs", src)

    assert client.objects[("ccc-test", "prefix/packs/pack.sqfs")] == b"pack-bytes"
    assert store.exists("prefix/packs/pack.sqfs") is True
    assert store.exists("prefix/missing") is False
    assert store.read_bytes("prefix/packs/pack.sqfs") == b"pack-bytes"

    out = tmp_path / "out.sqfs"
    store.get_file("prefix/packs/pack.sqfs", out)
    assert out.read_bytes() == b"pack-bytes"


def test_boto3_object_store_refuses_unsafe_keys(fake_store, tmp_path):
    store, _client = fake_store
    src = tmp_path / "data"
    src.write_bytes(b"x")

    with pytest.raises(ObjectStoreError, match="unsafe object key"):
        store.put_file("../escape", src)

    with pytest.raises(ObjectStoreError, match="unsafe object key"):
        store.read_bytes("")


def test_boto3_object_store_delete_prefix_removes_all_matching_keys(fake_store):
    store, client = fake_store
    store.put_bytes("prefix/a", b"a")
    store.put_bytes("prefix/b", b"b")
    store.put_bytes("other/c", b"c")

    deleted = store.delete_prefix("prefix/")

    assert deleted == 2
    assert store.exists("prefix/a") is False
    assert store.exists("prefix/b") is False
    assert store.exists("other/c") is True


def test_boto3_object_store_bucket_lifecycle(fake_store):
    store, client = fake_store

    assert store.bucket_exists() is False
    store.ensure_bucket(create=True)

    assert "ccc-test" in client.buckets
    assert store.bucket_exists() is True


def test_boto3_object_store_wraps_missing_download(fake_store, tmp_path):
    store, _client = fake_store

    with pytest.raises(ObjectStoreError, match="object not found"):
        store.get_file("missing/key", tmp_path / "out")
