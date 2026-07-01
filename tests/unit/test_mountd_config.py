from __future__ import annotations

import json

import pytest

from ccc_storage_core.manifest import WRITE_POLICY_LOCAL_SSD_ASYNC
from ccc_storage_mountd.config import ConfigError, MountdConfig
from ccc_storage_mountd.daemon import main as mountd_main


def test_mountd_config_loads_modular_toml_sections(tmp_path):
    path = tmp_path / "mountd.toml"
    path.write_text(
        """
[paths]
nfs_root = "/srv/ccc-storage"
run_dir = "/run/ccc-custom"
socket = "/run/ccc-custom/mountd.sock"
ready_file = "/run/ccc-custom/ready.json"
managed_parent = "/managed/datasets"
observe_root = "/storage/user/source"
observe_mountpoint = "/storage/user/published"
local_overlay_root = "/local/ssd/ccc-storage"

[runtime]
prefer_kernel = true
socket_mode = "0660"
observe_ready_timeout = 12.5

[defaults]
write_policy = "local-ssd-async"

[maintenance]
idle_unmount_ttl = 900
idle_reap_interval = 45
dirty_publish_interval = 2

[ownership]
uid = 2094
gid = 2094

[compaction]
interval_seconds = 3600
levels = "0:1G,1:128M,2:16M"
max_packs_per_level = 2
allow_base = true
after_commit = false
max_online_bytes = "512M"

[cold_storage]
backend = "s3"
enabled = true
archive_enabled = true
prefix = "ccc/custom/cold"
mirror_after_commit = true
remove_hot = false
idle_seconds = 123
interval_seconds = 456

[cold_storage.s3]
bucket = "ccc-bucket"
endpoint_url = "https://s3.example.invalid"
region_name = "us-test-1"
addressing_style = "path"
""".strip()
        + "\n"
    )

    cfg = MountdConfig.from_file(path)

    assert cfg.nfs_root == "/srv/ccc-storage"
    assert cfg.run_dir == "/run/ccc-custom"
    assert cfg.socket == "/run/ccc-custom/mountd.sock"
    assert cfg.ready_file == "/run/ccc-custom/ready.json"
    assert cfg.managed_parent == "/managed/datasets"
    assert cfg.observe_root == "/storage/user/source"
    assert cfg.observe_mountpoint == "/storage/user/published"
    assert cfg.local_overlay_root == "/local/ssd/ccc-storage"
    assert cfg.prefer_kernel is True
    assert cfg.socket_mode == "0660"
    assert cfg.observe_ready_timeout == 12.5
    assert cfg.default_write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC
    assert cfg.idle_unmount_ttl == 900
    assert cfg.idle_reap_interval == 45
    assert cfg.dirty_publish_interval == 2
    assert cfg.storage_uid == 2094
    assert cfg.storage_gid == 2094
    assert cfg.compaction_interval == 3600
    policy = cfg.level_policy()
    assert [level.max_bytes for level in policy.levels] == [1024**3, 128 * 1024**2, 16 * 1024**2]
    assert policy.max_packs_per_level == 2
    assert policy.allow_base_compaction is True
    assert policy.trigger_after_commit is False
    assert policy.max_online_compaction_bytes == 512 * 1024**2
    assert cfg.cold_storage.enabled is True
    assert cfg.cold_storage.archive_enabled is True
    assert cfg.cold_storage.prefix == "ccc/custom/cold"
    assert cfg.cold_storage.mirror_after_commit is True
    assert cfg.cold_storage.remove_hot is False
    assert cfg.cold_storage.idle_seconds == 123
    assert cfg.cold_storage.interval_seconds == 456
    assert cfg.cold_storage.bucket == "ccc-bucket"
    assert cfg.cold_storage.endpoint_url == "https://s3.example.invalid"
    assert cfg.cold_storage.region_name == "us-test-1"
    assert cfg.cold_storage.addressing_style == "path"


def test_mountd_config_env_overrides_file_without_client_config(tmp_path):
    path = tmp_path / "mountd.toml"
    path.write_text(
        """
[paths]
nfs_root = "/from/file"
run_dir = "/run/from-file"

[cold_storage]
enabled = false
prefix = "from-file"

[cold_storage.s3]
bucket = "file-bucket"
endpoint_url = "https://file.example.invalid"
""".strip()
        + "\n"
    )

    cfg = MountdConfig.from_file(path).with_env(
        {
            "CCC_NFS_ROOT": "/from/env",
            "CCC_NODE_RUN_DIR": "/run/from-env",
            "CCC_COLD_STORAGE_ENABLED": "1",
            "CCC_COLD_STORAGE_PREFIX": "from-env",
            "CCC_S3_BUCKET": "env-bucket",
            "CCC_S3_ENDPOINT": "https://env.example.invalid",
            "CCC_STORAGE_USER_ID": "1000",
            "CCC_STORAGE_GROUP_ID": "1001",
        }
    )

    assert cfg.nfs_root == "/from/env"
    assert cfg.run_dir == "/run/from-env"
    assert cfg.cold_storage.enabled is True
    assert cfg.cold_storage.prefix == "from-env"
    assert cfg.cold_storage.bucket == "env-bucket"
    assert cfg.cold_storage.endpoint_url == "https://env.example.invalid"
    assert cfg.storage_uid == 1000
    assert cfg.storage_gid == 1001


def test_mountd_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "mountd.toml"
    path.write_text("[paths]\nnfs_root = '/ok'\nunknown = 'typo'\n")

    with pytest.raises(ConfigError, match="unknown mountd config key"):
        MountdConfig.from_file(path)


def test_mountd_main_reads_config_file_and_allows_cli_override(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CCC_NFS_ROOT", raising=False)
    config_nfs = tmp_path / "config-nfs"
    cli_nfs = tmp_path / "cli-nfs"
    path = tmp_path / "mountd.toml"
    path.write_text(
        f"""
[paths]
nfs_root = "{config_nfs}"
run_dir = "{tmp_path / 'run'}"

[defaults]
write_policy = "local-ssd-async"

[ownership]
uid = 2094
gid = 2094
""".strip()
        + "\n"
    )

    assert mountd_main(["--config", str(path), "--nfs-root", str(cli_nfs), "--once-doctor"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["nfs_root"] == str(cli_nfs)
    assert data["default_write_policy"] == "local-ssd-async"
    assert data["storage_uid"] == 2094
    assert data["storage_gid"] == 2094


def test_mountd_help_does_not_validate_config_or_owner_env(capsys, monkeypatch):
    monkeypatch.setenv("USER_ID", "1234")
    monkeypatch.delenv("GROUP_ID", raising=False)
    monkeypatch.setenv("CCC_STORAGE_MOUNTD_CONFIG", "/missing/mountd.toml")

    with pytest.raises(SystemExit) as exc:
        mountd_main(["--help"])

    assert exc.value.code == 0
    assert "--config" in capsys.readouterr().out
