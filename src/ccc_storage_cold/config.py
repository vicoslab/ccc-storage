"""Cold-storage configuration and backend construction."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ccc_storage_cold.object_store import Boto3ObjectStore, ObjectStore, ObjectStoreError
from ccc_storage_core.names import safe_namespace_name

SIX_MONTHS_SECONDS = 180 * 24 * 60 * 60
ONE_WEEK_SECONDS = 7 * 24 * 60 * 60

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ColdStorageConfig:
    """Mountd-facing cold-storage configuration.

    `backend="s3"` is the only network backend today. Keeping it in this generic
    config makes future backends additive rather than forcing mountd/HPC code to
    know about S3 directly.
    """

    backend: str = "s3"
    prefix: str = "ccc-storage/cold"
    enabled: bool = False
    archive_enabled: bool = False
    mirror_after_commit: bool = False
    remove_hot: bool = True
    idle_seconds: float = SIX_MONTHS_SECONDS
    interval_seconds: float = ONE_WEEK_SECONDS
    bucket: str = ""
    endpoint_url: str = ""
    region_name: str = "us-east-1"
    addressing_style: str = "auto"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ColdStorageConfig:
        env = os.environ if env is None else env
        backend = env.get("CCC_COLD_STORAGE_BACKEND", "s3").strip() or "s3"
        bucket = env.get("CCC_COLD_STORAGE_BUCKET", env.get("CCC_S3_BUCKET", "")).strip()
        endpoint = env.get("CCC_COLD_STORAGE_ENDPOINT", env.get("CCC_S3_ENDPOINT", "")).strip()
        configured = bool(bucket and endpoint)
        return cls(
            backend=backend,
            prefix=env.get("CCC_COLD_STORAGE_PREFIX", "ccc-storage/cold").strip()
            or "ccc-storage/cold",
            enabled=_flag(env.get("CCC_COLD_STORAGE_ENABLED"), configured),
            archive_enabled=_flag(env.get("CCC_COLD_STORAGE_ARCHIVE_ENABLED"), configured),
            mirror_after_commit=_flag(env.get("CCC_COLD_STORAGE_MIRROR_AFTER_COMMIT"), False),
            remove_hot=_flag(env.get("CCC_COLD_STORAGE_REMOVE_HOT"), True),
            idle_seconds=_float_env(env, "CCC_COLD_STORAGE_IDLE_SECONDS", SIX_MONTHS_SECONDS),
            interval_seconds=_float_env(
                env,
                "CCC_COLD_STORAGE_INTERVAL_SECONDS",
                ONE_WEEK_SECONDS,
            ),
            bucket=bucket,
            endpoint_url=endpoint,
            region_name=env.get("CCC_COLD_STORAGE_REGION", env.get("CCC_S3_REGION", "us-east-1")),
            addressing_style=env.get(
                "CCC_COLD_STORAGE_ADDRESSING_STYLE",
                env.get("CCC_S3_ADDRESSING_STYLE", "auto"),
            ),
        )

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.backend == "s3" and self.bucket and self.endpoint_url)

    def build_store(self) -> ObjectStore | None:
        if not self.configured:
            return None
        if self.backend != "s3":
            raise ObjectStoreError(f"unsupported cold-storage backend: {self.backend}")
        return Boto3ObjectStore(
            bucket=self.bucket,
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
            addressing_style=self.addressing_style,
        )

    def child_prefix(self, child_id: str, generation: int) -> str:
        safe = safe_namespace_name(child_id)
        return f"{self.prefix.strip('/')}/children/{safe}/g{generation:04d}"


def hot_pack_dir(nfs_root: str | Path, child_id: str) -> Path:
    from ccc_storage_pack.builder import pack_object_dir

    return pack_object_dir(Path(nfs_root) / "packs", child_id)


def _flag(value: str | None, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in _TRUE


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return max(0.0, value)
