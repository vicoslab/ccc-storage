from __future__ import annotations

from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.manifest import ChildManifest, PackInfo, dump_atomic
from ccc_layered_pack import cli
from ccc_layered_pack.builder import BuildResult


def test_manifest_show_cli_outputs_json(tmp_path, capsys):
    manifest = ChildManifest(id="dataset:foo", name="foo", type="dataset", generation=1)
    path = tmp_path / "manifest.toml"
    dump_atomic(path, manifest)

    assert cli.main(["manifest", "show", str(path)]) == 0

    assert '"name": "foo"' in capsys.readouterr().out


def test_verify_cli_success(tmp_path, capsys):
    pack = tmp_path / "p.sqfs"
    pack.write_bytes(b"abc")

    assert (
        cli.main(
            ["verify", str(pack), "--sha256", sha256_file(pack), "--size", str(pack.stat().st_size)]
        )
        == 0
    )

    assert '"sha256"' in capsys.readouterr().out


def test_build_cli_writes_manifest(monkeypatch, tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out.sqfs"
    manifest = tmp_path / "manifest.toml"
    pack = PackInfo(path=str(out), sha256="b" * 64, size=5, file_count=1)

    def fake_build(*args, **kwargs):
        out.write_bytes(b"bytes")
        return BuildResult(pack=pack, args=("mksquashfs",))

    monkeypatch.setattr(cli, "build_pack", fake_build)

    assert cli.main([
        "build",
        str(src),
        str(out),
        "--manifest",
        str(manifest),
        "--child-id",
        "dataset:foo",
        "--name",
        "foo",
    ]) == 0

    assert manifest.exists()
    assert "dataset:foo" in manifest.read_text()
    assert '"manifest"' in capsys.readouterr().out
