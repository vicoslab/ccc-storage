from __future__ import annotations

from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, dump_atomic, load_manifest
from ccc_storage_core.protocol import Request
from ccc_storage_mountd import daemon
from ccc_storage_mountd.daemon import MountdService
from ccc_storage_mountd.workers.levels import LevelPolicy, parse_levels
from ccc_storage_pack.builder import BuildResult


def _pack(path, *, level, size, gen):
    return PackInfo(
        path=path,
        sha256=str(level) * 64,
        size=size,
        level=level,
        generation_min=gen,
        generation_max=gen,
        kind="base" if level == 0 else "delta",
    )


def _write_compactable_child(fake_nfs):
    base = _pack("/p/base.sqfs", level=0, size=100, gen=1)
    l2 = _pack("/p/l2.sqfs", level=2, size=20, gen=2)
    l3 = _pack("/p/l3.sqfs", level=3, size=80, gen=3)
    l4 = _pack("/p/l4.sqfs", level=4, size=8, gen=4)
    new = _pack("/p/new.sqfs", level=4, size=6, gen=5)
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=5,
        pack_stack=PackStack(active_revision="g5", lowers=(base, l2, l3, l4, new)),
    )
    manifest_path = fake_nfs.subdir("registry") / "foo.toml"
    dump_atomic(manifest_path, manifest)
    policy = LevelPolicy(levels=parse_levels("0:1000,1:500,2:100,3:94,4:10"))
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=fake_nfs.root / "run",
        level_policy=policy,
    )
    service.reload_registry()
    return service, manifest, manifest_path


def test_status_reports_level_compaction_details(fake_nfs):
    service, manifest, _ = _write_compactable_child(fake_nfs)

    status = service.handle_status(manifest.id)

    assert status["compaction"]["needed"] is True
    assert status["compaction"]["target_level"] == 3
    assert status["compaction"]["total_bytes"] == 94
    assert status["compaction"]["selected_packs"] == ["/p/l3.sqfs", "/p/l4.sqfs", "/p/new.sqfs"]
    assert status["compaction"]["blocked_reason"] == ""


def test_handle_compact_dry_run_returns_candidate_without_publishing(fake_nfs):
    service, manifest, manifest_path = _write_compactable_child(fake_nfs)

    result = service.handle_compact(manifest.id, dry_run=True)

    assert result["dry_run"] is True
    assert result["compaction"]["needed"] is True
    assert load_manifest(manifest_path).pack_stack.lowers == manifest.pack_stack.lowers


def test_handle_compact_builds_and_publishes_partial_pack(monkeypatch, fake_nfs):
    service, manifest, manifest_path = _write_compactable_child(fake_nfs)

    def fake_build_partial(selected, out_path, *, target_level, **kwargs):
        out_path.write_bytes(b"compact")
        return PackInfo(
            path=str(out_path),
            sha256=sha256_file(out_path),
            size=out_path.stat().st_size,
            level=target_level,
            generation_min=min(pack.generation_min for pack in selected),
            generation_max=max(pack.generation_max for pack in selected),
            kind="compact",
        )

    monkeypatch.setattr(daemon, "build_partial_compaction", fake_build_partial)

    result = service.handle_compact(manifest.id)

    assert result["compacted"] is True
    persisted = load_manifest(manifest_path)
    assert [pack.path for pack in persisted.pack_stack.lowers[:2]] == ["/p/base.sqfs", "/p/l2.sqfs"]
    assert len(persisted.pack_stack.lowers) == 3
    assert persisted.pack_stack.lowers[-1].level == 3
    assert result["retired_packs"] == ["/p/l3.sqfs", "/p/l4.sqfs", "/p/new.sqfs"]


def test_dispatch_compact_command(fake_nfs):
    service, manifest, _ = _write_compactable_child(fake_nfs)

    resp = service.dispatch(Request(command="compact", path=manifest.id, payload={"dry_run": True}))

    assert resp.ok
    assert resp.result["dry_run"] is True
    assert resp.result["compaction"]["needed"] is True


def test_blocked_compaction_is_reported_without_mutation(fake_nfs):
    base = _pack("/p/base.sqfs", level=0, size=60, gen=1)
    l1 = _pack("/p/l1.sqfs", level=1, size=8, gen=2)
    new = _pack("/p/new.sqfs", level=1, size=8, gen=3)
    manifest = ChildManifest(
        id="dataset:bar",
        name="bar",
        type="dataset",
        generation=3,
        pack_stack=PackStack(active_revision="g3", lowers=(base, l1, new)),
    )
    manifest_path = fake_nfs.subdir("registry") / "bar.toml"
    dump_atomic(manifest_path, manifest)
    service = MountdService(
        nfs_root=fake_nfs.ccc_storage,
        run_dir=fake_nfs.root / "run",
        level_policy=LevelPolicy(levels=parse_levels("0:100,1:10")),
    )
    service.reload_registry()

    result = service.handle_compact(manifest.id)

    assert result["compacted"] is False
    assert result["compaction"]["blocked_reason"]
    assert load_manifest(manifest_path).pack_stack.lowers == manifest.pack_stack.lowers


def test_commit_triggers_safe_compaction_after_publish(monkeypatch, fake_nfs):
    base = _pack("/p/base.sqfs", level=0, size=100, gen=1)
    l3 = _pack("/p/l3.sqfs", level=3, size=80, gen=2)
    l4 = _pack("/p/l4.sqfs", level=4, size=8, gen=3)
    manifest = ChildManifest(
        id="dataset:baz",
        name="baz",
        type="dataset",
        generation=3,
        pack_stack=PackStack(active_revision="g3", lowers=(base, l3, l4)),
    )
    manifest_path = fake_nfs.subdir("registry") / "baz.toml"
    dump_atomic(manifest_path, manifest)
    policy = LevelPolicy(
        levels=parse_levels("0:1000,1:500,2:100,3:94,4:10"),
        trigger_after_commit=True,
    )
    service = MountdService(fake_nfs.ccc_storage, fake_nfs.root / "run", level_policy=policy)
    service.reload_registry()
    upper = service.overlay_paths(manifest).active_upper
    upper.mkdir(parents=True, exist_ok=True)
    (upper / "new.txt").write_text("new")

    def fake_build_delta(src, base_manifest, out, tombstones=None):
        out.write_bytes(b"123456")
        return BuildResult(
            pack=PackInfo(path=str(out), sha256=sha256_file(out), size=6, file_count=1),
            args=("fake",),
        )

    def fake_build_partial(selected, out_path, *, target_level, **kwargs):
        out_path.write_bytes(b"compact")
        return PackInfo(
            path=str(out_path),
            sha256=sha256_file(out_path),
            size=94,
            level=target_level,
            generation_min=min(pack.generation_min for pack in selected),
            generation_max=max(pack.generation_max for pack in selected),
            kind="compact",
        )

    monkeypatch.setattr(daemon, "build_delta", fake_build_delta)
    monkeypatch.setattr(daemon, "build_partial_compaction", fake_build_partial)

    result = service.handle_commit(manifest.id)

    assert result["compaction"]["needed"] is False
    persisted = load_manifest(manifest_path)
    assert len(persisted.pack_stack.lowers) == 2
    assert persisted.pack_stack.lowers[-1].level == 3


def test_background_compaction_skips_rw_mounted_children(monkeypatch, fake_nfs):
    service, manifest, _ = _write_compactable_child(fake_nfs)
    monkeypatch.setattr(service.mounts, "status", lambda _manifest: {"mounted": True, "mode": "rw"})

    assert service.run_background_compaction_once() == []
