from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, OverlayInfo, PackInfo, PackStack, dump_atomic
from ccc_storage_core.observe import OBSERVE_MARKER_NAME
from ccc_storage_core.protocol import Request
from ccc_storage_mountd import childmount, daemon
from ccc_storage_mountd.control import ControlServer
from ccc_storage_mountd.daemon import MountdService
from ccc_storage_pack.builder import BuildResult


def _write_child(fake_nfs, *, child_id: str = "observe:env-a") -> ChildManifest:
    pack = fake_nfs.subdir("packs") / "base.sqfs"
    pack.write_bytes(b"base")
    manifest = ChildManifest(
        id=child_id,
        name="env-a",
        type="observed-child",
        generation=0,
        parent_path="env-a",
        overlay=OverlayInfo(
            mode="shared-overlay",
            active_upper=str(fake_nfs.ccc_storage / "overlays" / "observe%3Aenv-a" / "active"),
        ),
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    dump_atomic(fake_nfs.subdir("registry") / "env-a.toml", manifest)
    return manifest


class _FakeRwHandle:
    command = ("fake-fuse-overlayfs",)

    def __init__(self, mountpoint):
        self.mountpoint = mountpoint
        self.mounted = True

    def unmount(self) -> None:
        self.mounted = False


def test_control_socket_mode_is_applied(tmp_path):
    class Handler:
        def dispatch(self, request):  # pragma: no cover - not reached
            raise AssertionError(request)

    sock = tmp_path / "run" / "mountd.sock"
    server = ControlServer(sock, Handler(), socket_mode=0o660)
    server.start()
    try:
        assert stat.S_IMODE(sock.stat().st_mode) == 0o660
    finally:
        server.stop()


def test_commit_refuses_dirty_child_while_rw_mount_is_active(monkeypatch, fake_nfs, tmp_path):
    def fake_mount_layered_rw(packs, overlay_paths, mountpoint, **kwargs):
        return _FakeRwHandle(mountpoint)

    def fake_build_delta(src, base_manifest, out, tombstones=None):  # pragma: no cover
        out.write_bytes(b"delta")
        return BuildResult(
            pack=PackInfo(path=str(out), sha256=sha256_file(out), size=out.stat().st_size),
            args=("fake",),
        )

    monkeypatch.setattr(childmount, "mount_layered_rw", fake_mount_layered_rw)
    monkeypatch.setattr(daemon, "build_delta", fake_build_delta)
    manifest = _write_child(fake_nfs)
    service = MountdService(fake_nfs.ccc_storage, tmp_path / "run")
    service.reload_registry()
    service.mounts.mount_rw(manifest)
    paths = service.overlay_paths(manifest)
    paths.active_upper.mkdir(parents=True, exist_ok=True)
    (paths.active_upper / "dirty.txt").write_text("dirty")

    response = service.dispatch(Request(command="commit", path=manifest.id))

    assert response.ok is False
    assert response.code == "EBUSY"
    assert "mounted" in response.error.lower()
    assert (paths.active_upper / "dirty.txt").exists()


def test_mountd_docker_artifacts_are_dedicated_service_container():
    root = Path(__file__).resolve().parents[2]
    dockerfile = root / "deploy" / "docker" / "mountd.Dockerfile"
    entrypoint = root / "deploy" / "docker" / "mountd-entrypoint.sh"
    example_config = root / "deploy" / "config" / "mountd.example.toml"
    smoke = root / "dev" / "validation" / "docker" / "mountd-container-runtime-smoke.sh"

    assert dockerfile.exists()
    assert entrypoint.exists()
    assert example_config.exists()
    assert smoke.exists()

    docker_text = dockerfile.read_text()
    assert "ccc-storage doctor" in docker_text
    assert "make test" not in docker_text
    assert ".[manifest,fuse]" in docker_text
    assert "ENTRYPOINT" in docker_text
    assert "HEALTHCHECK" in docker_text
    assert "tini" in docker_text

    entry_text = entrypoint.read_text()
    assert "CCC_STORAGE_MOUNTD_CONFIG" in entry_text
    assert "--config" in entry_text
    for var in ("CCC_NFS_ROOT", "CCC_OBSERVE_ROOT", "CCC_OBSERVE_MOUNTPOINT"):
        assert var in entry_text
    assert "exec ccc-storage mountd" in entry_text
    assert "--observe-mountpoint" in entry_text
    assert "--socket-mode" in entry_text
    assert "--storage-uid" in entry_text
    assert "--storage-gid" in entry_text
    assert "CCC_STORAGE_USER_ID" in entry_text
    assert "CCC_STORAGE_GROUP_ID" in entry_text
    assert "USER_ID" in entry_text
    assert "GROUP_ID" in entry_text
    assert "/var/run/docker.sock" not in entry_text

    smoke_text = smoke.read_text()
    assert "ccc-storage-mountd-test" in smoke_text
    assert "ccc-storage-app-test" in smoke_text
    assert "bind-propagation=rshared" in smoke_text
    assert "bind-propagation=rslave" in smoke_text
    assert "--device /dev/fuse" in smoke_text
    assert "--cap-add SYS_ADMIN" in smoke_text
    assert "CCC_MOUNTD_STORAGE_USER_ID:-2094" in smoke_text
    assert "CCC_MOUNTD_STORAGE_GROUP_ID:-2094" in smoke_text
    assert "CCC_STORAGE_USER_ID" in smoke_text
    assert "CCC_STORAGE_GROUP_ID" in smoke_text
    app_section = smoke_text.split('--name "$app_name"', 1)[1]
    assert "--cap-add SYS_ADMIN" not in app_section
    assert "CCC_MOUNTD_SOCK" not in app_section


def test_idle_mount_reaper_unmounts_released_mount(monkeypatch, fake_nfs, tmp_path):
    def fake_mount_stack_ro(packs, mountpoint, prefer_kernel=False):
        return _FakeRwHandle(mountpoint)

    monkeypatch.setattr(childmount, "mount_stack_ro", fake_mount_stack_ro)
    manifest = _write_child(fake_nfs)
    service = MountdService(fake_nfs.ccc_storage, tmp_path / "run")
    service.reload_registry()

    service.handle_mount(manifest.id)
    assert service.handle_status(manifest.id)["mounted"] is True
    service.mounts.release(manifest.id)

    assert service.reap_idle_mounts(0.0) == []
    service.mounts._records[manifest.id].last_used -= 10
    assert service.reap_idle_mounts(0.001) == [manifest.id]
    assert service.handle_status(manifest.id)["mounted"] is False


def test_ready_file_contains_doctor_json(fake_nfs, tmp_path):
    service = MountdService(
        fake_nfs.ccc_storage,
        tmp_path / "run",
        observe_mountpoint=tmp_path / "published",
    )
    ready = tmp_path / "status" / "ready.json"

    daemon._write_ready_file(service, ready)

    data = json.loads(ready.read_text())
    assert data["nfs_root"] == str(fake_nfs.ccc_storage)
    assert data["observation_mountpoint"] == str(tmp_path / "published")
    assert data["active_submount_count"] == 0


def test_mountd_service_chowns_observation_state_to_configured_client_owner(
    monkeypatch,
    fake_nfs,
    tmp_path,
):
    chowned: list[tuple[Path, int, int, bool]] = []

    def record_chown(path, uid, gid, *, follow_symlinks=True):
        chowned.append((Path(path), uid, gid, follow_symlinks))

    monkeypatch.setattr(os, "chown", record_chown)
    source = tmp_path / "source"
    source.mkdir()
    (source / OBSERVE_MARKER_NAME).write_text("")
    service = MountdService(
        fake_nfs.ccc_storage,
        tmp_path / "run",
        observe_root=source,
        storage_uid=2094,
        storage_gid=2094,
    )

    status = service.handle_observe_mkdir("env-a")

    paths = {path for path, uid, gid, _follow in chowned if (uid, gid) == (2094, 2094)}
    assert source / "env-a" in paths
    assert fake_nfs.ccc_storage / "registry" / "observe" / "env-a.toml" in paths
    assert Path(status["overlay"]["active_upper"]) in paths


def test_mountd_loop_runs_background_compaction_interval(monkeypatch):
    class FakeServer:
        def __init__(self):
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    class FakeService:
        def __init__(self):
            self.compactions = 0
            self.stopped = False

        def publish_dirty_epochs(self):
            return []

        def reap_idle_mounts(self, ttl):
            return []

        def run_background_compaction_once(self):
            self.compactions += 1
            return []

        def stop(self):
            self.stopped = True

    server = FakeServer()
    service = FakeService()
    monotonic_values = iter([0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(monotonic_values, 1.0))

    def stop_after_one_tick(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", stop_after_one_tick)

    with pytest.raises(KeyboardInterrupt):
        daemon._serve_forever(
            server,
            service,
            dirty_publish_interval=0,
            compaction_interval=0.1,
        )

    assert server.started is True
    assert server.stopped is True
    assert service.stopped is True
    assert service.compactions == 1


def test_mountd_cli_exposes_production_safety_flags():
    text = Path(daemon.__file__).read_text()
    from ccc_storage_mountd import config as mountd_config

    config_text = Path(mountd_config.__file__).read_text()
    combined_text = text + "\n" + config_text
    for flag in (
        "--config",
        "--socket-mode",
        "--prefer-kernel",
        "--observe-ready-timeout",
        "--ready-file",
        "--idle-unmount-ttl",
        "--idle-reap-interval",
        "--compaction-interval",
        "--storage-uid",
        "--storage-gid",
    ):
        assert flag in text
    assert "CCC_STORAGE_MOUNTD_CONFIG" in combined_text
    assert "CCC_COMPACT_INTERVAL_SECONDS" in combined_text
    assert "CCC_STORAGE_USER_ID" in combined_text
    assert "CCC_STORAGE_GROUP_ID" in combined_text
    assert "USER_ID" in combined_text
    assert "GROUP_ID" in combined_text
    assert "run_background_compaction_once" in text
