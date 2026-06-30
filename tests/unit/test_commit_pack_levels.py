from __future__ import annotations

from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack, dump_atomic, load_manifest
from ccc_layered_mountd import daemon
from ccc_layered_mountd.daemon import MountdService
from ccc_layered_mountd.workers.levels import LevelPolicy, parse_levels
from ccc_layered_pack.builder import BuildResult


def test_commit_assigns_delta_level_and_generation_metadata(monkeypatch, fake_nfs):
    base = fake_nfs.subdir("packs") / "base.sqfs"
    base.write_bytes(b"base")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=7,
        pack_stack=PackStack(
            lowers=(PackInfo(path=str(base), sha256=sha256_file(base), size=base.stat().st_size),)
        ),
    )
    manifest_path = fake_nfs.subdir("registry") / "foo.toml"
    dump_atomic(manifest_path, manifest)
    policy = LevelPolicy(levels=parse_levels("0:1M,1:10K,2:10"))
    service = MountdService(
        nfs_root=fake_nfs.ccc_layered,
        run_dir=fake_nfs.root / "run",
        level_policy=policy,
    )
    service.reload_registry()
    upper = service.overlay_paths(manifest).active_upper
    upper.mkdir(parents=True, exist_ok=True)
    (upper / "new.txt").write_text("new")

    def fake_build_delta(src, base_manifest, out, tombstones=None):
        out.write_bytes(b"12345")
        return BuildResult(
            pack=PackInfo(
                path=str(out),
                sha256=sha256_file(out),
                size=out.stat().st_size,
                file_count=1,
            ),
            args=("fake",),
        )

    monkeypatch.setattr(daemon, "build_delta", fake_build_delta)

    service.handle_commit(manifest.id)

    persisted = load_manifest(manifest_path)
    delta = persisted.pack_stack.lowers[-1]
    assert delta.kind == "delta"
    assert delta.generation_min == 8
    assert delta.generation_max == 8
    assert delta.level == 2
