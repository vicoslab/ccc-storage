"""Mountd-owned configuration file support.

The unprivileged ``ccc-storage`` client intentionally stays small: it talks to
mountd over the local socket and does not need the full storage policy/S3
configuration.  This module is for the privileged per-node mountd process.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ccc_storage_cold.config import (
    ONE_WEEK_SECONDS,
    SIX_MONTHS_SECONDS,
    ColdStorageConfig,
)
from ccc_storage_core.manifest import (
    WRITE_POLICY_SHARED_NFS,
    ManifestError,
    normalize_write_policy,
)
from ccc_storage_mountd.workers.levels import (
    DEFAULT_LEVELS_SPEC,
    DEFAULT_MAX_ONLINE_COMPACTION,
    LevelPolicy,
    LevelPolicyError,
    parse_human_bytes,
    parse_levels,
)

DEFAULT_CONFIG_PATH = Path("/etc/ccc-storage/mountd.toml")
CONFIG_ENV_VAR = "CCC_STORAGE_MOUNTD_CONFIG"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised for invalid mountd configuration files or values."""


@dataclass(frozen=True)
class ObservationDirConfig:
    """A public observation directory and its persistent state subdir."""

    path: str
    state_subdir: str = ".ccc-storage"


@dataclass(frozen=True)
class MountdConfig:
    """Resolved mountd configuration before final CLI overrides.

    Precedence is handled by ``load`` + argparse in ``daemon.main``:
    built-in defaults < TOML config < environment < explicit CLI flags.
    """

    nfs_root: str = ""
    run_dir: str = "/run/ccc-storage"
    socket: str = ""
    ready_file: str = ""
    managed_parent: str = ""
    observe_root: str = ""
    observe_mountpoint: str = ""
    observation_dirs: tuple[ObservationDirConfig, ...] = ()
    local_overlay_root: str = ""

    prefer_kernel: bool = False
    socket_mode: str = "0600"
    observe_ready_timeout: float = 10.0

    default_write_policy: str = WRITE_POLICY_SHARED_NFS

    idle_unmount_ttl: float = 300.0
    idle_reap_interval: float = 30.0
    dirty_publish_interval: float = 1.0

    storage_uid: int | None = None
    storage_gid: int | None = None

    compaction_interval: float = 0.0
    pack_levels: str = DEFAULT_LEVELS_SPEC
    max_packs_per_level: int = 1
    allow_base_compaction: bool = False
    compact_after_commit: bool = True
    max_online_compaction_bytes: str | int = DEFAULT_MAX_ONLINE_COMPACTION

    cold_storage: ColdStorageConfig = field(default_factory=ColdStorageConfig)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        *,
        use_default_path: bool = True,
    ) -> MountdConfig:
        """Load config from an explicit/standard path, then overlay env vars.

        If *path* is omitted, ``$CCC_STORAGE_MOUNTD_CONFIG`` is honored.  If both
        are unset and ``/etc/ccc-storage/mountd.toml`` exists, that standard file
        is loaded.  Missing implicit standard files are ignored; missing explicit
        files are configuration errors.
        """

        env = os.environ if env is None else env
        selected = str(path or env.get(CONFIG_ENV_VAR, "")).strip()
        if selected:
            config = cls.from_file(selected)
        elif use_default_path and DEFAULT_CONFIG_PATH.exists():
            config = cls.from_file(DEFAULT_CONFIG_PATH)
        else:
            config = cls()
        return config.with_env(env)

    @classmethod
    def from_file(cls, path: str | Path) -> MountdConfig:
        config_path = Path(path)
        try:
            raw = tomllib.loads(config_path.read_text())
        except FileNotFoundError as exc:
            raise ConfigError(f"mountd config file not found: {config_path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in mountd config {config_path}: {exc}") from exc
        if not isinstance(raw, dict):  # pragma: no cover - tomllib always returns dict
            raise ConfigError(f"mountd config {config_path} must be a TOML table")
        return cls().with_mapping(raw, source=str(config_path))

    def with_mapping(
        self, data: Mapping[str, Any], *, source: str = "mountd config"
    ) -> MountdConfig:
        """Return a copy with values from a parsed TOML mapping applied."""

        _check_keys(
            data,
            "",
            {
                "paths",
                "runtime",
                "defaults",
                "maintenance",
                "ownership",
                "compaction",
                "cold_storage",
                "s3",
                "observation_dirs",
            },
            source,
        )
        cfg = self

        cfg = replace(
            cfg,
            observation_dirs=_observation_dirs_from_mapping(
                data.get("observation_dirs", ()),
                source,
            ),
        )

        paths = _table(data, "paths", source)
        _check_keys(
            paths,
            "paths",
            {
                "nfs_root",
                "run_dir",
                "socket",
                "ready_file",
                "managed_parent",
                "observe_root",
                "observe_mountpoint",
                "local_overlay_root",
            },
            source,
        )
        cfg = _replace_if_present(
            cfg,
            paths,
            source,
            {
                "nfs_root": ("nfs_root", _as_str),
                "run_dir": ("run_dir", _as_str),
                "socket": ("socket", _as_str),
                "ready_file": ("ready_file", _as_str),
                "managed_parent": ("managed_parent", _as_str),
                "observe_root": ("observe_root", _as_str),
                "observe_mountpoint": ("observe_mountpoint", _as_str),
                "local_overlay_root": ("local_overlay_root", _as_str),
            },
            section="paths",
        )

        runtime = _table(data, "runtime", source)
        _check_keys(
            runtime, "runtime", {"prefer_kernel", "socket_mode", "observe_ready_timeout"}, source
        )
        cfg = _replace_if_present(
            cfg,
            runtime,
            source,
            {
                "prefer_kernel": ("prefer_kernel", _as_bool),
                "socket_mode": ("socket_mode", _as_str),
                "observe_ready_timeout": ("observe_ready_timeout", _as_nonnegative_float),
            },
            section="runtime",
        )

        defaults = _table(data, "defaults", source)
        _check_keys(defaults, "defaults", {"write_policy", "default_write_policy"}, source)
        if "write_policy" in defaults or "default_write_policy" in defaults:
            value = defaults.get("write_policy", defaults.get("default_write_policy"))
            cfg = replace(
                cfg,
                default_write_policy=_normalize_write_policy(
                    value, f"{source}: defaults.write_policy"
                ),
            )

        maintenance = _table(data, "maintenance", source)
        _check_keys(
            maintenance,
            "maintenance",
            {"idle_unmount_ttl", "idle_reap_interval", "dirty_publish_interval"},
            source,
        )
        cfg = _replace_if_present(
            cfg,
            maintenance,
            source,
            {
                "idle_unmount_ttl": ("idle_unmount_ttl", _as_nonnegative_float),
                "idle_reap_interval": ("idle_reap_interval", _as_nonnegative_float),
                "dirty_publish_interval": ("dirty_publish_interval", _as_nonnegative_float),
            },
            section="maintenance",
        )

        ownership = _table(data, "ownership", source)
        _check_keys(ownership, "ownership", {"uid", "gid", "user_id", "group_id"}, source)
        if "uid" in ownership or "user_id" in ownership:
            cfg = replace(
                cfg,
                storage_uid=_as_optional_owner_id(
                    ownership.get("uid", ownership.get("user_id")),
                    f"{source}: ownership.uid",
                ),
            )
        if "gid" in ownership or "group_id" in ownership:
            cfg = replace(
                cfg,
                storage_gid=_as_optional_owner_id(
                    ownership.get("gid", ownership.get("group_id")),
                    f"{source}: ownership.gid",
                ),
            )

        compaction = _table(data, "compaction", source)
        _check_keys(
            compaction,
            "compaction",
            {
                "interval_seconds",
                "levels",
                "max_packs_per_level",
                "allow_base",
                "allow_base_compaction",
                "after_commit",
                "max_online_bytes",
                "max_online_compaction_bytes",
            },
            source,
        )
        cfg = _replace_if_present(
            cfg,
            compaction,
            source,
            {
                "interval_seconds": ("compaction_interval", _as_nonnegative_float),
                "levels": ("pack_levels", _as_str),
                "max_packs_per_level": ("max_packs_per_level", _as_positive_int),
                "allow_base": ("allow_base_compaction", _as_bool),
                "allow_base_compaction": ("allow_base_compaction", _as_bool),
                "after_commit": ("compact_after_commit", _as_bool),
                "max_online_bytes": ("max_online_compaction_bytes", _as_human_bytes_string),
                "max_online_compaction_bytes": (
                    "max_online_compaction_bytes",
                    _as_human_bytes_string,
                ),
            },
            section="compaction",
        )

        cfg = replace(cfg, cold_storage=_cold_from_mapping(cfg.cold_storage, data, source))
        # Validate derived policies while the file path is still available for a
        # useful error message.
        cfg.level_policy()
        if (cfg.storage_uid is None) != (cfg.storage_gid is None):
            raise ConfigError(
                f"{source}: configure both ownership.uid and ownership.gid, or neither"
            )
        return cfg

    def with_env(self, env: Mapping[str, str] | None = None) -> MountdConfig:
        """Return a copy with legacy/current ``CCC_*`` mountd env vars applied."""

        env = os.environ if env is None else env
        cfg = self
        cfg = _env_replace_str(cfg, env, "CCC_NFS_ROOT", "nfs_root")
        cfg = _env_replace_str(cfg, env, "CCC_NODE_RUN_DIR", "run_dir")
        cfg = _env_replace_str(cfg, env, "CCC_MOUNTD_SOCK", "socket")
        cfg = _env_replace_str(cfg, env, "CCC_MOUNTD_READY_FILE", "ready_file")
        cfg = _env_replace_str(cfg, env, "CCC_MANAGED_PARENT", "managed_parent")
        cfg = _env_replace_str(cfg, env, "CCC_OBSERVE_ROOT", "observe_root")
        cfg = _env_replace_str(cfg, env, "CCC_OBSERVE_MOUNTPOINT", "observe_mountpoint")
        if _env_has(env, "CCC_OBSERVATION_DIRS"):
            state_subdir = env.get("CCC_OBSERVATION_STATE_SUBDIR", ".ccc-storage").strip()
            if not state_subdir:
                state_subdir = ".ccc-storage"
            cfg = replace(
                cfg,
                observation_dirs=tuple(
                    ObservationDirConfig(path=part.strip(), state_subdir=state_subdir)
                    for part in env["CCC_OBSERVATION_DIRS"].split(":")
                    if part.strip()
                ),
            )
        cfg = _env_replace_str(cfg, env, "CCC_LOCAL_OVERLAY_ROOT", "local_overlay_root")
        cfg = _env_replace_bool(cfg, env, "CCC_PREFER_KERNEL", "prefer_kernel")
        cfg = _env_replace_str(cfg, env, "CCC_MOUNTD_SOCKET_MODE", "socket_mode")
        cfg = _env_replace_float(
            cfg,
            env,
            "CCC_OBSERVE_READY_TIMEOUT",
            "observe_ready_timeout",
        )
        if _env_has(env, "CCC_DEFAULT_WRITE_POLICY"):
            cfg = replace(
                cfg,
                default_write_policy=_normalize_write_policy(
                    env["CCC_DEFAULT_WRITE_POLICY"], "$CCC_DEFAULT_WRITE_POLICY"
                ),
            )
        cfg = _env_replace_float(cfg, env, "CCC_IDLE_UNMOUNT_TTL", "idle_unmount_ttl")
        cfg = _env_replace_float(cfg, env, "CCC_IDLE_REAP_INTERVAL", "idle_reap_interval")
        cfg = _env_replace_float(
            cfg,
            env,
            "CCC_DIRTY_PUBLISH_INTERVAL",
            "dirty_publish_interval",
        )
        cfg = _env_replace_float(
            cfg,
            env,
            "CCC_COMPACT_INTERVAL_SECONDS",
            "compaction_interval",
        )
        cfg = _env_replace_str(cfg, env, "CCC_PACK_LEVELS", "pack_levels")
        cfg = _env_replace_int(cfg, env, "CCC_MAX_PACKS_PER_LEVEL", "max_packs_per_level")
        cfg = _env_replace_bool(
            cfg,
            env,
            "CCC_ALLOW_BASE_COMPACTION",
            "allow_base_compaction",
        )
        cfg = _env_replace_bool(cfg, env, "CCC_COMPACT_AFTER_COMMIT", "compact_after_commit")
        cfg = _env_replace_human_bytes(
            cfg,
            env,
            "CCC_MAX_ONLINE_COMPACTION_BYTES",
            "max_online_compaction_bytes",
        )
        storage_uid = _env_first(env, "CCC_STORAGE_USER_ID", "USER_ID")
        storage_gid = _env_first(env, "CCC_STORAGE_GROUP_ID", "GROUP_ID")
        if storage_uid is not None:
            cfg = replace(
                cfg, storage_uid=_as_optional_owner_id(storage_uid, "$CCC_STORAGE_USER_ID")
            )
        if storage_gid is not None:
            cfg = replace(
                cfg, storage_gid=_as_optional_owner_id(storage_gid, "$CCC_STORAGE_GROUP_ID")
            )
        cfg = replace(cfg, cold_storage=_cold_from_env(cfg.cold_storage, env))
        cfg.level_policy()
        if (cfg.storage_uid is None) != (cfg.storage_gid is None):
            raise ConfigError("configure both CCC_STORAGE_USER_ID/CCC_STORAGE_GROUP_ID or neither")
        return cfg

    def level_policy(self) -> LevelPolicy:
        """Build the pack-level policy represented by this config."""

        try:
            return LevelPolicy(
                levels=parse_levels(self.pack_levels),
                max_packs_per_level=self.max_packs_per_level,
                allow_base_compaction=self.allow_base_compaction,
                max_online_compaction_bytes=parse_human_bytes(self.max_online_compaction_bytes),
                trigger_after_commit=self.compact_after_commit,
                trigger_interval_seconds=self.compaction_interval,
            )
        except (LevelPolicyError, ValueError) as exc:
            raise ConfigError(f"invalid compaction policy in mountd config: {exc}") from exc


def _table(data: Mapping[str, Any], key: str, source: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: mountd config section [{key}] must be a table")
    return value


def _check_keys(data: Mapping[str, Any], section: str, allowed: set[str], source: str) -> None:
    prefix = f"{section}." if section else ""
    for key in data:
        if key not in allowed:
            raise ConfigError(f"{source}: unknown mountd config key {prefix}{key}")


def _replace_if_present(
    cfg: MountdConfig,
    table: Mapping[str, Any],
    source: str,
    mapping: Mapping[str, tuple[str, Any]],
    *,
    section: str,
) -> MountdConfig:
    updates: dict[str, Any] = {}
    for key, (attr, parser) in mapping.items():
        if key in table:
            updates[attr] = parser(table[key], f"{source}: {section}.{key}")
    return replace(cfg, **updates) if updates else cfg


def _observation_dirs_from_mapping(value: Any, source: str) -> tuple[ObservationDirConfig, ...]:
    if value in (None, (), []):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{source}: observation_dirs must be an array of tables")
    parsed: list[ObservationDirConfig] = []
    for index, item in enumerate(value):
        label = f"observation_dirs[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{source}: {label} must be a table")
        _check_keys(item, label, {"path", "state_subdir"}, source)
        if "path" not in item:
            raise ConfigError(f"{source}: {label}.path is required")
        path = _as_str(item["path"], f"{source}: {label}.path")
        if not path:
            raise ConfigError(f"{source}: {label}.path must not be empty")
        state_subdir = _as_str(
            item.get("state_subdir", ".ccc-storage"),
            f"{source}: {label}.state_subdir",
        )
        if not state_subdir:
            raise ConfigError(f"{source}: {label}.state_subdir must not be empty")
        if "/" in state_subdir or state_subdir in {".", ".."}:
            raise ConfigError(f"{source}: {label}.state_subdir must be a directory name")
        parsed.append(ObservationDirConfig(path=path, state_subdir=state_subdir))
    return tuple(parsed)


def _cold_from_mapping(
    base: ColdStorageConfig,
    data: Mapping[str, Any],
    source: str,
) -> ColdStorageConfig:
    cold = _table(data, "cold_storage", source)
    _check_keys(
        cold,
        "cold_storage",
        {
            "backend",
            "enabled",
            "archive_enabled",
            "prefix",
            "mirror_after_commit",
            "remove_hot",
            "idle_seconds",
            "interval_seconds",
            "s3",
        },
        source,
    )
    s3 = _table(cold, "s3", source)
    top_s3 = _table(data, "s3", source)
    _check_keys(
        s3,
        "cold_storage.s3",
        {"bucket", "endpoint", "endpoint_url", "region", "region_name", "addressing_style"},
        source,
    )
    _check_keys(
        top_s3,
        "s3",
        {"bucket", "endpoint", "endpoint_url", "region", "region_name", "addressing_style"},
        source,
    )
    bucket = _optional_table_str(s3, "bucket", source, "cold_storage.s3.bucket")
    endpoint = _first_table_str(
        s3,
        ("endpoint_url", "endpoint"),
        source,
        "cold_storage.s3.endpoint_url",
    )
    region = _first_table_str(s3, ("region_name", "region"), source, "cold_storage.s3.region_name")
    addressing = _optional_table_str(
        s3, "addressing_style", source, "cold_storage.s3.addressing_style"
    )
    # Legacy/simple top-level [s3] can be merged with a [cold_storage] section.
    bucket = bucket or _optional_table_str(top_s3, "bucket", source, "s3.bucket")
    endpoint = endpoint or _first_table_str(
        top_s3, ("endpoint_url", "endpoint"), source, "s3.endpoint_url"
    )
    region = region or _first_table_str(top_s3, ("region_name", "region"), source, "s3.region_name")
    addressing = addressing or _optional_table_str(
        top_s3, "addressing_style", source, "s3.addressing_style"
    )

    updates: dict[str, Any] = {}
    if "backend" in cold:
        updates["backend"] = _as_str(cold["backend"], f"{source}: cold_storage.backend") or "s3"
    if "prefix" in cold:
        updates["prefix"] = (
            _as_str(cold["prefix"], f"{source}: cold_storage.prefix") or "ccc-storage/cold"
        )
    if "enabled" in cold:
        updates["enabled"] = _as_bool(cold["enabled"], f"{source}: cold_storage.enabled")
    if "archive_enabled" in cold:
        updates["archive_enabled"] = _as_bool(
            cold["archive_enabled"],
            f"{source}: cold_storage.archive_enabled",
        )
    if "mirror_after_commit" in cold:
        updates["mirror_after_commit"] = _as_bool(
            cold["mirror_after_commit"],
            f"{source}: cold_storage.mirror_after_commit",
        )
    if "remove_hot" in cold:
        updates["remove_hot"] = _as_bool(
            cold["remove_hot"],
            f"{source}: cold_storage.remove_hot",
        )
    if "idle_seconds" in cold:
        updates["idle_seconds"] = _as_nonnegative_float(
            cold["idle_seconds"],
            f"{source}: cold_storage.idle_seconds",
        )
    if "interval_seconds" in cold:
        updates["interval_seconds"] = _as_nonnegative_float(
            cold["interval_seconds"],
            f"{source}: cold_storage.interval_seconds",
        )
    if bucket is not None:
        updates["bucket"] = bucket
    if endpoint is not None:
        updates["endpoint_url"] = endpoint
    if region is not None:
        updates["region_name"] = region
    if addressing is not None:
        updates["addressing_style"] = addressing

    configured = bool(
        updates.get("bucket", base.bucket) and updates.get("endpoint_url", base.endpoint_url)
    )
    if configured and "enabled" not in cold:
        updates["enabled"] = True
    if configured and "archive_enabled" not in cold:
        updates["archive_enabled"] = True
    return replace(base, **updates) if updates else base


def _cold_from_env(base: ColdStorageConfig, env: Mapping[str, str]) -> ColdStorageConfig:
    updates: dict[str, Any] = {}
    backend = _env_first(env, "CCC_COLD_STORAGE_BACKEND")
    if backend is not None:
        updates["backend"] = backend.strip() or "s3"
    prefix = _env_first(env, "CCC_COLD_STORAGE_PREFIX")
    if prefix is not None:
        updates["prefix"] = prefix.strip() or "ccc-storage/cold"
    bucket = _env_first(env, "CCC_COLD_STORAGE_BUCKET", "CCC_S3_BUCKET")
    if bucket is not None:
        updates["bucket"] = bucket.strip()
    endpoint = _env_first(env, "CCC_COLD_STORAGE_ENDPOINT", "CCC_S3_ENDPOINT")
    if endpoint is not None:
        updates["endpoint_url"] = endpoint.strip()
    region = _env_first(env, "CCC_COLD_STORAGE_REGION", "CCC_S3_REGION")
    if region is not None:
        updates["region_name"] = region.strip() or "us-east-1"
    addressing = _env_first(env, "CCC_COLD_STORAGE_ADDRESSING_STYLE", "CCC_S3_ADDRESSING_STYLE")
    if addressing is not None:
        updates["addressing_style"] = addressing.strip() or "auto"
    configured = bool(
        updates.get("bucket", base.bucket) and updates.get("endpoint_url", base.endpoint_url)
    )
    enabled = _env_first(env, "CCC_COLD_STORAGE_ENABLED")
    if enabled is not None:
        updates["enabled"] = _as_bool(enabled, "$CCC_COLD_STORAGE_ENABLED")
    elif configured and not base.enabled:
        updates["enabled"] = True
    archive_enabled = _env_first(env, "CCC_COLD_STORAGE_ARCHIVE_ENABLED")
    if archive_enabled is not None:
        updates["archive_enabled"] = _as_bool(
            archive_enabled,
            "$CCC_COLD_STORAGE_ARCHIVE_ENABLED",
        )
    elif configured and not base.archive_enabled:
        updates["archive_enabled"] = True
    mirror = _env_first(env, "CCC_COLD_STORAGE_MIRROR_AFTER_COMMIT")
    if mirror is not None:
        updates["mirror_after_commit"] = _as_bool(
            mirror,
            "$CCC_COLD_STORAGE_MIRROR_AFTER_COMMIT",
        )
    remove_hot = _env_first(env, "CCC_COLD_STORAGE_REMOVE_HOT")
    if remove_hot is not None:
        updates["remove_hot"] = _as_bool(remove_hot, "$CCC_COLD_STORAGE_REMOVE_HOT")
    idle = _env_first(env, "CCC_COLD_STORAGE_IDLE_SECONDS")
    if idle is not None:
        updates["idle_seconds"] = _as_nonnegative_float(idle, "$CCC_COLD_STORAGE_IDLE_SECONDS")
    interval = _env_first(env, "CCC_COLD_STORAGE_INTERVAL_SECONDS")
    if interval is not None:
        updates["interval_seconds"] = _as_nonnegative_float(
            interval,
            "$CCC_COLD_STORAGE_INTERVAL_SECONDS",
        )
    return replace(base, **updates) if updates else base


def _env_has(env: Mapping[str, str], key: str) -> bool:
    return key in env and str(env[key]).strip() != ""


def _env_first(env: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = env.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _replace_attr(cfg: MountdConfig, attr: str, value: Any) -> MountdConfig:
    return replace(cfg, **{attr: value})  # type: ignore[arg-type]


def _env_replace_str(
    cfg: MountdConfig, env: Mapping[str, str], key: str, attr: str
) -> MountdConfig:
    value = _env_first(env, key)
    return _replace_attr(cfg, attr, value.strip()) if value is not None else cfg


def _env_replace_bool(
    cfg: MountdConfig, env: Mapping[str, str], key: str, attr: str
) -> MountdConfig:
    value = _env_first(env, key)
    return _replace_attr(cfg, attr, _as_bool(value, f"${key}")) if value is not None else cfg


def _env_replace_float(
    cfg: MountdConfig, env: Mapping[str, str], key: str, attr: str
) -> MountdConfig:
    value = _env_first(env, key)
    return (
        _replace_attr(cfg, attr, _as_nonnegative_float(value, f"${key}"))
        if value is not None
        else cfg
    )


def _env_replace_int(
    cfg: MountdConfig, env: Mapping[str, str], key: str, attr: str
) -> MountdConfig:
    value = _env_first(env, key)
    return (
        _replace_attr(cfg, attr, _as_positive_int(value, f"${key}")) if value is not None else cfg
    )


def _env_replace_human_bytes(
    cfg: MountdConfig,
    env: Mapping[str, str],
    key: str,
    attr: str,
) -> MountdConfig:
    value = _env_first(env, key)
    return (
        _replace_attr(cfg, attr, _as_human_bytes_string(value, f"${key}"))
        if value is not None
        else cfg
    )


def _optional_table_str(
    table: Mapping[str, Any],
    key: str,
    source: str,
    label: str,
) -> str | None:
    if key not in table:
        return None
    return _as_str(table[key], f"{source}: {label}")


def _first_table_str(
    table: Mapping[str, Any],
    keys: tuple[str, ...],
    source: str,
    label: str,
) -> str | None:
    for key in keys:
        if key in table:
            return _as_str(table[key], f"{source}: {label}")
    return None


def _as_str(value: Any, label: str) -> str:
    if isinstance(value, (dict, list, tuple)):
        raise ConfigError(f"{label} must be a string")
    return str(value).strip()


def _as_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"{label} must be a boolean")


def _as_nonnegative_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a non-negative number") from exc
    if parsed < 0:
        raise ConfigError(f"{label} must be a non-negative number")
    return parsed


def _as_positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return parsed


def _as_optional_owner_id(value: Any, label: str) -> int | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text, 10)
    except ValueError as exc:
        raise ConfigError(f"{label} must be a non-negative integer") from exc
    if parsed < 0:
        raise ConfigError(f"{label} must be a non-negative integer")
    return parsed


def _as_human_bytes_string(value: Any, label: str) -> str | int:
    if isinstance(value, int):
        parse_human_bytes(value)
        return value
    text = _as_str(value, label)
    try:
        parse_human_bytes(text)
    except LevelPolicyError as exc:
        raise ConfigError(f"{label} must be a byte size such as '10G' or 1048576") from exc
    return text


def _normalize_write_policy(value: Any, label: str) -> str:
    try:
        return normalize_write_policy(None if value is None else str(value))
    except ManifestError as exc:
        raise ConfigError(f"{label}: {exc}") from exc


# Explicit construction keeps defaults discoverable in one place for docs/examples.
DEFAULT_MOUNTD_CONFIG = MountdConfig(
    cold_storage=ColdStorageConfig(
        backend="s3",
        prefix="ccc-storage/cold",
        enabled=False,
        archive_enabled=False,
        mirror_after_commit=False,
        remove_hot=True,
        idle_seconds=SIX_MONTHS_SECONDS,
        interval_seconds=ONE_WEEK_SECONDS,
    )
)
