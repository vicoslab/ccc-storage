from __future__ import annotations

from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, load_manifest
from ccc_storage_mountd import daemon
from ccc_storage_mountd.daemon import MountdService
from ccc_storage_mountd.overlay import dirty_stats
from ccc_storage_pack.builder import BuildResult


def _write_dirty_child(fake_nfs):
    pack = fake_nfs.subdir("packs") / "base.sqfs"
    pack.write_bytes(b"base")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=7,
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    manifest_path = fake_nfs.subdir("registry") / "foo.toml"
    from ccc_storage_core.manifest import dump_atomic

    dump_atomic(manifest_path, manifest)
    service = MountdService(nfs_root=fake_nfs.ccc_storage, run_dir=fake_nfs.root / "run")
    service.reload_registry()
    upper = service.overlay_paths(manifest).active_upper
    upper.mkdir(parents=True, exist_ok=True)
    (upper / "new.txt").write_text("new")
    return service, manifest, manifest_path


def test_mountd_status_reports_dirty_overlay_stats(fake_nfs):
    service, manifest, _manifest_path = _write_dirty_child(fake_nfs)

    status = service.handle_status(manifest.id)

    assert status["state"] == "dirty"
    assert status["overlay"]["file_count"] == 1
    assert status["overlay"]["bytes"] == 3


def test_dirty_stats_ignore_fuse_overlayfs_whiteout_artifacts(tmp_path):
    upper = tmp_path / "upper"
    upper.mkdir()
    (upper / "client.txt").write_text("client")
    (upper / ".wh.deleted").write_text("")
    opaque_dir = upper / "new-dir"
    opaque_dir.mkdir()
    (opaque_dir / ".wh..wh..opq").write_text("")

    stats = dirty_stats(upper)

    assert stats.dirty is True
    assert stats.file_count == 1
    assert stats.bytes == len("client")


def test_manual_commit_builds_delta_publishes_manifest_and_clears_sealed_overlay(
    monkeypatch,
    fake_nfs,
):
    service, manifest, manifest_path = _write_dirty_child(fake_nfs)
    built = []

    def fake_build_delta(src, base_manifest, out, tombstones=None):
        out.write_bytes(b"delta")
        pack = PackInfo(
            path=str(out),
            sha256=sha256_file(out),
            size=out.stat().st_size,
            file_count=1,
        )
        built.append((src, base_manifest.id, out))
        return BuildResult(pack=pack, args=("fake",))

    monkeypatch.setattr(daemon, "build_delta", fake_build_delta)

    result = service.handle_commit(manifest.id, message="manual")

    assert result["generation"] == 8
    assert result["state"] == "clean"
    assert built
    persisted = load_manifest(manifest_path)
    assert persisted.generation == 8
    assert len(persisted.pack_stack.lowers) == 2
    assert persisted.pack_stack.lowers[-1].sha256 == sha256_file(
        persisted.pack_stack.lowers[-1].path
    )
    assert not any(service.overlay_paths(persisted).sealed_dir.glob("g0008-*"))
