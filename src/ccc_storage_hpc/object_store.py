"""Object-store abstractions for local tests and real S3-compatible backends."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol


class ObjectStoreError(RuntimeError):
    """Raised when an object-store operation fails."""


class ObjectStore(Protocol):
    """Small object-store protocol used by S3 mirror/recall helpers."""

    def put_file(self, key: str, source: str | Path) -> None: ...

    def get_file(self, key: str, dest: str | Path) -> None: ...

    def read_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


@dataclass(frozen=True)
class S3Config:
    bucket: str
    endpoint_url: str
    region_name: str = "us-east-1"
    addressing_style: str = "auto"
    signature_version: str = "s3v4"
    request_checksum_calculation: str = "when_required"
    response_checksum_validation: str = "when_required"


class LocalObjectStore:
    """A deterministic, no-network object store backed by local files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        clean = _clean_key(key)
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


class Boto3ObjectStore:
    """S3-compatible object store backed by boto3.

    The constructor accepts an injected client for unit tests. Without one,
    boto3 is imported lazily so the base package still imports without the S3
    optional dependency installed.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region_name: str = "us-east-1",
        addressing_style: str = "auto",
        signature_version: str = "s3v4",
        request_checksum_calculation: str = "when_required",
        response_checksum_validation: str = "when_required",
        client: Any | None = None,
    ) -> None:
        if not bucket:
            raise ObjectStoreError("S3 bucket is required")
        if not endpoint_url:
            raise ObjectStoreError("S3 endpoint_url is required")
        self.config = S3Config(
            bucket=bucket,
            endpoint_url=endpoint_url,
            region_name=region_name,
            addressing_style=addressing_style,
            signature_version=signature_version,
            request_checksum_calculation=request_checksum_calculation,
            response_checksum_validation=response_checksum_validation,
        )
        self.client = client if client is not None else _make_boto3_client(self.config)

    @property
    def bucket(self) -> str:
        return self.config.bucket

    def put_file(self, key: str, source: str | Path) -> None:
        clean = _clean_key(key)
        src = Path(source)
        if not src.is_file():
            raise ObjectStoreError(f"source file does not exist: {src}")
        try:
            with src.open("rb") as f:
                self.client.put_object(Bucket=self.bucket, Key=clean, Body=f)
        except Exception as exc:  # pragma: no cover - exercised with real boto3
            msg = f"failed to upload object {clean}: {_error_message(exc)}"
            raise ObjectStoreError(msg) from exc

    def put_bytes(self, key: str, data: bytes) -> None:
        clean = _clean_key(key)
        try:
            self.client.put_object(Bucket=self.bucket, Key=clean, Body=data)
        except Exception as exc:  # pragma: no cover - exercised with real boto3
            msg = f"failed to upload object {clean}: {_error_message(exc)}"
            raise ObjectStoreError(msg) from exc

    def get_file(self, key: str, dest: str | Path) -> None:
        clean = _clean_key(key)
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=clean)
            body: BinaryIO = obj["Body"]
            with out.open("wb") as f:
                f.write(body.read())
        except Exception as exc:
            if _client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectStoreError(f"object not found: {clean}") from exc
            msg = f"failed to download object {clean}: {_error_message(exc)}"
            raise ObjectStoreError(msg) from exc

    def read_bytes(self, key: str) -> bytes:
        clean = _clean_key(key)
        try:
            return self.client.get_object(Bucket=self.bucket, Key=clean)["Body"].read()
        except Exception as exc:
            if _client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectStoreError(f"object not found: {clean}") from exc
            raise ObjectStoreError(f"failed to read object {clean}: {_error_message(exc)}") from exc

    def exists(self, key: str) -> bool:
        clean = _clean_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=clean)
            return True
        except Exception as exc:
            if _client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise ObjectStoreError(f"failed to stat object {clean}: {_error_message(exc)}") from exc

    def bucket_exists(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except Exception as exc:
            if _client_error_code(exc) in {"404", "NoSuchBucket", "NotFound"}:
                return False
            msg = f"failed to stat bucket {self.bucket}: {_error_message(exc)}"
            raise ObjectStoreError(msg) from exc

    def ensure_bucket(self, *, create: bool = False) -> None:
        if self.bucket_exists():
            return
        if not create:
            raise ObjectStoreError(f"bucket does not exist: {self.bucket}")
        try:
            self.client.create_bucket(Bucket=self.bucket)
        except Exception as exc:
            msg = f"failed to create bucket {self.bucket}: {_error_message(exc)}"
            raise ObjectStoreError(msg) from exc

    def delete_prefix(self, prefix: str) -> int:
        clean = _clean_key(prefix)
        if not clean.endswith("/"):
            clean += "/"
        deleted = 0
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": clean}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = self.client.list_objects_v2(**kwargs)
            except Exception as exc:  # pragma: no cover - exercised with real boto3
                msg = f"failed to list prefix {clean}: {_error_message(exc)}"
                raise ObjectStoreError(msg) from exc
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                try:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})
                except Exception as exc:  # pragma: no cover - exercised with real boto3
                    raise ObjectStoreError(
                        f"failed to delete prefix {clean}: {_error_message(exc)}"
                    ) from exc
                deleted += len(objects)
            if not page.get("IsTruncated"):
                return deleted
            token = page.get("NextContinuationToken")


def _make_boto3_client(config: S3Config) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - optional dependency path
        raise ObjectStoreError("boto3/botocore are required for S3 object storage") from exc

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region_name,
        config=Config(
            signature_version=config.signature_version,
            request_checksum_calculation=config.request_checksum_calculation,
            response_checksum_validation=config.response_checksum_validation,
            s3={"addressing_style": config.addressing_style},
        ),
    )


def _clean_key(key: str) -> str:
    clean = key.strip("/")
    if not clean or ".." in Path(clean).parts:
        raise ObjectStoreError(f"unsafe object key: {key!r}")
    return clean


def _client_error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


def _error_message(exc: BaseException) -> str:
    code = _client_error_code(exc)
    if code:
        return f"{code}: {exc}"
    return str(exc)
