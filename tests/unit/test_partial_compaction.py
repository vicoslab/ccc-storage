from __future__ import annotations

from pathlib import Path

from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_layered_mountd.workers.compaction import (
    build_partial_compaction,
    publish_partial_compaction,
)
from ccc_layered_pack.builder import BuildResult


def _pack(path: str, *, level: int, gen_min: int, gen_max: int | None = None, size: int = 10):
    return PackInfo(
        path=path,
        sha256=str(level) * 64,
        size=size,
        level=level,
        generation_min=gen_min,
        generation_max=gen_min if gen_max is None else gen_max,
        kind="base" if level == 0 else "delta",
    )


def test_publish_partial_compaction_replaces_only_selected_newer_packs():
    base = _pack("/p/base.sqfs", level=0, gen_min=1)
    l2 = _pack("/p/l2.sqfs", level=2, gen_min=2, gen_max=4)
    l3 = _pack("/p/l3.sqfs", level=3, gen_min=5, gen_max=6)
    l4 = _pack("/p/l4.sqfs", level=4, gen_min=7)
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=7,
        pack_stack=PackStack(active_revision="g7", lowers=(base, l2, l3, l4)),
    )
    new_pack = _pack("/p/compact-l3.sqfs", level=0, gen_min=0, size=20)

    updated, retired = publish_partial_compaction(
        manifest,
        selected=(l3, l4),
        new_pack=new_pack,
        target_level=3,
    )

    assert updated.pack_stack.lowers == (
        base,
        l2,
        PackInfo(
            path="/p/compact-l3.sqfs",
            sha256="0" * 64,
            size=20,
            level=3,
            generation_min=5,
            generation_max=7,
            kind="compact",
        ),
    )
    assert retired == (l3, l4)
    assert updated.generation == 7
    assert updated.pack_stack.active_revision == "g7"


def test_build_partial_compaction_materializes_selected_packs_old_to_new(tmp_path):
    selected = (
        _pack("/p/l3.sqfs", level=3, gen_min=5),
        _pack("/p/l4.sqfs", level=4, gen_min=6),
    )
    out = tmp_path / "compact.sqfs"
    extracted: list[tuple[str, Path]] = []

    def fake_extract(pack_path, dest):
        extracted.append((str(pack_path), Path(dest)))
        marker = Path(dest) / f"{Path(pack_path).stem}.txt"
        marker.write_text("extracted")

    def fake_build(src, dest, **kwargs):
        assert (Path(src) / "l3.txt").exists()
        assert (Path(src) / "l4.txt").exists()
        Path(dest).write_bytes(b"compact")
        return BuildResult(
            pack=PackInfo(
                path=str(dest),
                sha256=sha256_file(dest),
                size=Path(dest).stat().st_size,
            ),
            args=("fake",),
        )

    pack = build_partial_compaction(
        selected,
        out,
        target_level=3,
        extract_fn=fake_extract,
        build_pack_fn=fake_build,
    )

    assert [item[0] for item in extracted] == ["/p/l3.sqfs", "/p/l4.sqfs"]
    assert pack.path == str(out)
    assert pack.level == 3
    assert pack.generation_min == 5
    assert pack.generation_max == 6
    assert pack.kind == "compact"


def test_build_partial_compaction_applies_whiteouts_within_selected_group(tmp_path):
    selected = (
        _pack("/p/old.sqfs", level=3, gen_min=5),
        _pack("/p/delete.sqfs", level=4, gen_min=6),
    )
    out = tmp_path / "compact.sqfs"
    layer_sources = {
        "/p/old.sqfs": {"victim.txt": "old", "kept.txt": "kept"},
        "/p/delete.sqfs": {".wh.victim.txt": "", "new.txt": "new"},
    }

    def fake_extract(pack_path, dest):
        dest = Path(dest)
        for rel, text in layer_sources[str(pack_path)].items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def fake_build(src, dest, **kwargs):
        src = Path(src)
        assert not (src / "victim.txt").exists()
        assert (src / ".wh.victim.txt").exists()
        assert (src / "kept.txt").read_text() == "kept"
        assert (src / "new.txt").read_text() == "new"
        Path(dest).write_bytes(b"compact")
        return BuildResult(
            pack=PackInfo(
                path=str(dest),
                sha256=sha256_file(dest),
                size=Path(dest).stat().st_size,
            ),
            args=("fake",),
        )

    build_partial_compaction(
        selected,
        out,
        target_level=3,
        extract_fn=fake_extract,
        build_pack_fn=fake_build,
    )
