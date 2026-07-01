from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ccc_storage_core.manifest import PackInfo, PackStack, dump_atomic, load_manifest
from ccc_storage_core.observe import OBSERVE_MARKER_NAME
from ccc_storage_mountd import childmount
from ccc_storage_mountd.config import ObservationDirConfig
from ccc_storage_mountd.daemon import MountdService


class FakeHandle:
    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.command = ("fake",)
        self.mounted = True

    def unmount(self):
        self.mounted = False


def test_observe_mkdir_registers_child_manifest_without_mounting(fake_nfs, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")

    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
    )

    status = service.handle_observe_mkdir("user1")

    assert status["id"] == "observe:user1"
    assert status["parent_path"] == "user1"
    assert status["mounted"] is False
    assert (source / "user1").is_dir()
    manifest = load_manifest(fake_nfs.ccc_storage / "registry" / "observe" / "user1.toml")
    assert manifest.id == "observe:user1"
    assert manifest.pack_stack.lowers == ()
    assert service.mounts.active_count() == 0


def test_observe_mkdir_uses_observation_dir_state(tmp_path):
    observation = tmp_path / "observed"
    observation.mkdir()
    legacy_state = tmp_path / "legacy-state"

    service = MountdService(
        nfs_root=legacy_state,
        run_dir=tmp_path / "run",
        observation_dirs=(ObservationDirConfig(path=str(observation)),),
    )

    status = service.handle_observe_mkdir(str(observation / "user1"))

    assert status["id"].startswith("observe:")
    assert (observation / "user1").is_dir()
    assert (observation / ".ccc-storage" / "registry" / "observe" / "user1.toml").is_file()
    assert not (legacy_state / "registry" / "observe" / "user1.toml").exists()


def test_observation_router_uses_lexical_paths_without_resolve(monkeypatch, tmp_path):
    observation = tmp_path / "observed"
    observation.mkdir()

    def forbidden_resolve(self, *args, **kwargs):
        raise AssertionError("Path.resolve() would stat/re-enter the FUSE mount")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)
    service = MountdService(
        nfs_root=tmp_path / "legacy-state",
        run_dir=tmp_path / "run",
        observation_dirs=(ObservationDirConfig(path=str(observation)),),
    )

    status = service.handle_observe_mkdir(str(observation / "user1"))

    assert status["parent_path"] == "user1"


def test_observe_roots_with_same_child_name_do_not_collide(tmp_path):
    obs_a = tmp_path / "a"
    obs_b = tmp_path / "b"
    obs_a.mkdir()
    obs_b.mkdir()

    service = MountdService(
        nfs_root=tmp_path / "legacy-state",
        run_dir=tmp_path / "run",
        observation_dirs=(
            ObservationDirConfig(path=str(obs_a)),
            ObservationDirConfig(path=str(obs_b)),
        ),
    )

    status_a = service.handle_observe_mkdir(str(obs_a / "shared"))
    status_b = service.handle_observe_mkdir(str(obs_b / "shared"))

    manifest_a = obs_a / ".ccc-storage" / "registry" / "observe" / "shared.toml"
    manifest_b = obs_b / ".ccc-storage" / "registry" / "observe" / "shared.toml"
    assert manifest_a.is_file()
    assert manifest_b.is_file()
    assert status_a["id"] != status_b["id"]
    assert status_a["overlay"]["active_upper"].startswith(
        str(obs_a / ".ccc-storage" / "overlays")
    )
    assert status_b["overlay"]["active_upper"].startswith(
        str(obs_b / ".ccc-storage" / "overlays")
    )


def test_nested_observation_dir_uses_nearest_root_state(tmp_path):
    top = tmp_path / "top"
    nested = top / "user1" / "conda" / "envs"
    nested.mkdir(parents=True)

    service = MountdService(
        nfs_root=tmp_path / "legacy-state",
        run_dir=tmp_path / "run",
        observation_dirs=(
            ObservationDirConfig(path=str(top)),
            ObservationDirConfig(path=str(nested)),
        ),
    )

    service.handle_observe_mkdir(str(top / "user1"))
    env_status = service.handle_observe_mkdir(str(nested / "env-a"))

    assert env_status["parent_path"] == "env-a"
    assert (nested / ".ccc-storage" / "registry" / "observe" / "env-a.toml").is_file()
    assert not (top / ".ccc-storage" / "registry" / "observe" / "envs_env-a.toml").exists()
    assert not (
        top / ".ccc-storage" / "registry" / "observe" / "user1_conda_envs_env-a.toml"
    ).exists()


def test_service_status_scans_all_observation_dir_registries(tmp_path):
    obs_a = tmp_path / "a"
    obs_b = tmp_path / "b"
    obs_a.mkdir()
    obs_b.mkdir()
    service = MountdService(
        nfs_root=tmp_path / "legacy-state",
        run_dir=tmp_path / "run",
        observation_dirs=(
            ObservationDirConfig(path=str(obs_a)),
            ObservationDirConfig(path=str(obs_b)),
        ),
    )
    service.handle_observe_mkdir(str(obs_b / "shared"))

    listed = service.handle_ls()["children"]
    status = service.handle_status("shared")

    assert [item["parent_path"] for item in listed] == ["shared"]
    assert status["overlay"]["active_upper"].startswith(
        str(obs_b / ".ccc-storage" / "overlays")
    )


def test_observe_ls_reports_discovered_and_registered_children(fake_nfs, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    (source / "user1").mkdir()

    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    before = service.handle_observe_ls()["children"]
    assert before == [
        {
            "id": "observe:user1",
            "path": "user1",
            "registered": False,
            "generation": 0,
            "mounted": False,
            "status": None,
        }
    ]

    service.handle_observe_mkdir("user1")
    after = service.handle_observe_ls()["children"]
    assert after[0]["registered"] is True
    assert after[0]["generation"] == 0
    assert after[0]["mounted"] is False
    assert after[0]["status"]["id"] == "observe:user1"


def test_observe_access_mounts_only_requested_child_and_nested_roots_work(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        calls.append((tuple(pack.path for pack in packs), overlay_paths, mountpoint))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    nested = source / "user1" / "conda"
    nested.mkdir(parents=True)
    (nested / OBSERVE_MARKER_NAME).write_text("")

    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    user1 = service.handle_observe_mkdir("user1")
    user2 = service.handle_observe_mkdir("user2")
    env_a = service.handle_observe_mkdir("user1/conda/env-a")

    for status in (user1, user2, env_a):
        manifest_path = (
            fake_nfs.ccc_storage / "registry" / "observe" / f"{status['safe_name']}.toml"
        )
        manifest = load_manifest(manifest_path)
        pack_path = fake_nfs.ccc_storage / "packs" / status["safe_name"] / "base.sqfs"
        pack_path.parent.mkdir(parents=True, exist_ok=True)
        pack_path.write_bytes(status["id"].encode())
        updated = replace(
            manifest,
            generation=1,
            pack_stack=PackStack(
                active_revision="g1",
                lowers=(
                    PackInfo(
                        path=str(pack_path),
                        sha256="a" * 64,
                        size=pack_path.stat().st_size,
                    ),
                ),
            ),
        )
        dump_atomic(manifest_path, updated)

    assert service.mounts.active_count() == 0

    mounted = service.handle_observe_access("user1/conda/env-a/bin/python")

    assert mounted["id"] == "observe:user1/conda/env-a"
    assert mounted["mounted"] is True
    assert service.mounts.active_ids() == ["observe:user1/conda/env-a"]
    assert len(calls) == 1


def test_observe_access_generation0_child_mounts_writable_upper_without_pack(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    calls = []

    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, prefer_kernel=False, **kwargs):
        calls.append((tuple(packs), overlay_paths, mountpoint, prefer_kernel))
        return FakeHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw, raising=False)
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")

    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=tmp_path / "run",
        observe_root=source,
    )
    service.handle_observe_mkdir("new-env")

    mounted = service.handle_observe_access("new-env/bin/python")

    assert mounted["id"] == "observe:new-env"
    assert mounted["mounted"] is True
    assert len(calls) == 1
    assert calls[0][0] == ()
