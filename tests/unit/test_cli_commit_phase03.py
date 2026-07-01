from __future__ import annotations

from ccc_storage_cli.main import main as cli_main
from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, dump_atomic
from ccc_storage_mountd.control import ControlServer
from ccc_storage_mountd.daemon import MountdService
from ccc_storage_pack.builder import BuildResult


def test_cli_commit_roundtrip_over_control_socket(fake_nfs, tmp_path, monkeypatch, capsys):
    pack = fake_nfs.subdir("packs") / "base.sqfs"
    pack.write_bytes(b"base")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    dump_atomic(fake_nfs.subdir("registry") / "foo.toml", manifest)
    service = MountdService(nfs_root=fake_nfs.ccc_storage, run_dir=tmp_path / "run")
    service.reload_registry()
    upper = service.overlay_paths(manifest).active_upper
    upper.mkdir(parents=True, exist_ok=True)
    (upper / "new.txt").write_text("new")

    from ccc_storage_mountd import daemon

    def fake_build_delta(src, base_manifest, out, tombstones=None):
        out.write_bytes(b"delta")
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
    sock = tmp_path / "mountd.sock"
    server = ControlServer(sock, service)
    server.start()
    monkeypatch.setenv("CCC_MOUNTD_SOCK", str(sock))
    try:
        assert cli_main(["commit", "dataset:foo", "--json"]) == 0
        out = capsys.readouterr().out
        assert '"generation": 2' in out
        assert '"state": "clean"' in out
    finally:
        server.stop()
