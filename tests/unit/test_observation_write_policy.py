from __future__ import annotations

from ccc_storage_cli import conda_shim
from ccc_storage_core.manifest import (
    WRITE_POLICY_LOCAL_SSD_ASYNC,
    WRITE_POLICY_SHARED_NFS,
    load_manifest,
)
from ccc_storage_core.observe import OBSERVE_MARKER_NAME, parse_observe_marker_policy
from ccc_storage_mountd.daemon import MountdService


def test_observe_marker_empty_uses_mountd_default_policy(fake_nfs, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")

    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
        default_write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC,
    )

    service.handle_observe_mkdir("env-a")
    manifest = load_manifest(fake_nfs.ccc_storage / "registry" / "observe" / "env-a.toml")

    assert manifest.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC


def test_observe_marker_toml_policy_overrides_mountd_default(fake_nfs, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text('write_policy = "local-ssd-async"\n')

    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
        default_write_policy=WRITE_POLICY_SHARED_NFS,
    )

    service.handle_observe_mkdir("env-a")
    manifest = load_manifest(fake_nfs.ccc_storage / "registry" / "observe" / "env-a.toml")

    assert manifest.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC


def test_parse_observe_marker_accepts_plain_policy(tmp_path):
    marker = tmp_path / OBSERVE_MARKER_NAME
    marker.write_text("local-ssd-async\n")

    assert parse_observe_marker_policy(marker) == WRITE_POLICY_LOCAL_SSD_ASYNC


def test_init_conda_envs_can_set_default_write_policy(tmp_path):
    marker = conda_shim.init_conda_envs(
        tmp_path / "envs",
        write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC,
    )

    assert marker.read_text() == 'write_policy = "local-ssd-async"\n'
