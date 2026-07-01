from __future__ import annotations

from pathlib import Path

import pytest

from ccc_storage_cli import conda_shim
from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, dump_atomic, load_manifest
from ccc_storage_mountd import daemon
from ccc_storage_mountd.env_txn import CommandResult, EnvUpdateContext
from ccc_storage_pack.builder import BuildResult


def _fake_build_delta(src, base_manifest, out, tombstones=None):
    out.write_bytes(b"delta")
    return BuildResult(
        pack=PackInfo(path=str(out), sha256=sha256_file(out), size=out.stat().st_size),
        args=("fake",),
    )


def _write_env(fake_nfs, name="myenv") -> Path:
    pack = fake_nfs.subdir("packs") / f"{name}-base.sqfs"
    pack.write_bytes(b"base")
    manifest = ChildManifest(
        id=f"conda-env:{name}",
        name=name,
        type="conda-env",
        generation=0,
        parent_path=f"conda/envs/{name}",
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    path = fake_nfs.subdir("registry") / f"{name}.toml"
    dump_atomic(path, manifest)
    return path


def test_conda_shim_falls_back_when_unmanaged(tmp_path):
    calls = []

    def run_real(argv, env):
        calls.append((argv, env.copy()))
        return 17

    code = conda_shim.run_shim(
        "conda",
        ["install", "numpy"],
        env={"CCC_MOUNTD_SOCK": str(tmp_path / "missing.sock")},
        run_real=run_real,
    )

    assert code == 17
    assert calls == [
        (["conda", "install", "numpy"], {"CCC_MOUNTD_SOCK": str(tmp_path / "missing.sock")})
    ]


def test_conda_shim_disable_forces_fallback():
    calls = []

    def run_real(argv, env):
        calls.append(argv)
        return 0

    assert conda_shim.run_shim(
        "mamba",
        ["install", "numpy"],
        env={"CCC_STORAGE_SHIM_DISABLE": "1", "CCC_STORAGE_ENV_SELECTOR": "myenv"},
        run_real=run_real,
    ) == 0
    assert calls == [["mamba", "install", "numpy"]]


def test_conda_shim_non_mutating_command_falls_back():
    calls = []

    def run_real(argv, env):
        calls.append(argv)
        return 0

    assert conda_shim.run_shim(
        "conda",
        ["list", "-n", "myenv"],
        env={"CCC_STORAGE_ENV_SELECTOR": "myenv"},
        run_real=run_real,
    ) == 0
    assert calls == [["conda", "list", "-n", "myenv"]]


def test_conda_shim_managed_success_commits(monkeypatch, fake_nfs):
    manifest_path = _write_env(fake_nfs)
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    seen = []

    def run_managed(ctx: EnvUpdateContext, argv: list[str]) -> CommandResult:
        seen.append((ctx.env_id, argv))
        (ctx.active_upper / "lib" / "numpy.py").parent.mkdir(parents=True, exist_ok=True)
        (ctx.active_upper / "lib" / "numpy.py").write_text("ok")
        return CommandResult(returncode=0)

    code = conda_shim.run_shim(
        "conda",
        ["install", "numpy"],
        env={
            "CCC_NFS_ROOT": str(fake_nfs.ccc_storage),
            "CCC_STORAGE_ENV_SELECTOR": "myenv",
        },
        run_real=lambda argv, env: pytest.fail("should not fall back"),
        run_managed=run_managed,
    )

    assert code == 0
    assert seen == [("conda-env:myenv", ["conda", "install", "numpy"])]
    assert load_manifest(manifest_path).generation == 1


def test_conda_shim_managed_failure_preserves_overlay(monkeypatch, fake_nfs):
    manifest_path = _write_env(fake_nfs)
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)

    def run_managed(ctx: EnvUpdateContext, argv: list[str]) -> CommandResult:
        (ctx.active_upper / "partial.txt").write_text("partial")
        return CommandResult(returncode=9)

    code = conda_shim.run_shim(
        "mamba",
        ["remove", "bad"],
        env={
            "CCC_NFS_ROOT": str(fake_nfs.ccc_storage),
            "CCC_STORAGE_ENV_SELECTOR": "myenv",
        },
        run_real=lambda argv, env: pytest.fail("should not fall back"),
        run_managed=run_managed,
    )

    assert code == 9
    assert load_manifest(manifest_path).generation == 0
    partial = fake_nfs.ccc_storage / "overlays" / "conda-env%3Amyenv" / "active" / "partial.txt"
    assert partial.exists()


def test_init_conda_envs_marker_is_idempotent(tmp_path):
    root = tmp_path / "conda" / "envs"

    conda_shim.init_conda_envs(root)
    conda_shim.init_conda_envs(root)

    assert root.is_dir()
    assert (root / "CCC_STORAGE_OBSERVE").read_text() == ""
