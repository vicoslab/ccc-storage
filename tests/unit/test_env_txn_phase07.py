"""Phase 07 — conda env transaction foundation.

A committed env is a read-only SquashFS child (no overlay in the import path);
a package transaction takes an exclusive per-env update lock, writes to the
overlay, runs a sanity check, and commits-on-success / preserves-on-failure.

These tests use the fake-NFS tree and *fake* command runners / sanity checkers
only — they never invoke real conda/mamba/pip (Phase 07 constraint). The commit
delta build is monkeypatched the same way the phase-03/05 commit tests do.
"""

from __future__ import annotations

import argparse

import pytest

from ccc_layered_cli import env as env_cli
from ccc_layered_core.checksum import sha256_file
from ccc_layered_core.manifest import (
    ChildManifest,
    PackInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)
from ccc_layered_mountd import daemon
from ccc_layered_mountd.daemon import MountdService, _safe_child_name
from ccc_layered_mountd.env_txn import (
    CommandResult,
    EnvTransaction,
    EnvUpdateContext,
    env_status,
)
from ccc_layered_pack.builder import BuildResult


def _fake_build_delta(src, base_manifest, out, tombstones=None):
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


def _write_env(fake_nfs, *, name="myenv", generation=2):
    pack = fake_nfs.subdir("packs") / f"{name}-base.sqfs"
    pack.write_bytes(b"base")
    manifest = ChildManifest(
        id=f"conda-env:{name}",
        name=name,
        type="conda-env",
        generation=generation,
        parent_path=f"conda/envs/{name}",
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    manifest_path = fake_nfs.subdir("registry") / f"{name}.toml"
    dump_atomic(manifest_path, manifest)
    service = MountdService(nfs_root=fake_nfs.ccc_layered, run_dir=fake_nfs.root / "run")
    service.reload_registry()
    return service, manifest, manifest_path


def _update_lock_path(fake_nfs, manifest):
    return fake_nfs.ccc_layered / "locks" / f"{_safe_child_name(manifest.id)}.update.lock"


# --- lock lifecycle ---------------------------------------------------------


def test_env_txn_holds_exclusive_update_lock_during_run_and_releases(fake_nfs):
    service, manifest, _ = _write_env(fake_nfs)
    lock_path = _update_lock_path(fake_nfs, manifest)
    seen = {}

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        seen["held"] = lock_path.exists()
        return CommandResult(returncode=0)

    EnvTransaction(service, manifest.id, runner=runner).run(["conda", "install", "numpy"])

    assert seen["held"] is True
    assert not lock_path.exists()


def test_concurrent_env_txn_is_blocked_and_does_not_run_command(fake_nfs):
    service, manifest, manifest_path = _write_env(fake_nfs)
    lock_path = _update_lock_path(fake_nfs, manifest)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{}")
    ran = []

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        ran.append(ctx)
        return CommandResult(returncode=0)

    result = EnvTransaction(service, manifest.id, runner=runner).run(["pip", "install", "x"])

    assert result.status == "blocked"
    assert result.committed is False
    assert result.overlay_preserved is False
    assert "lock" in result.message.lower()
    assert ran == []
    assert load_manifest(manifest_path).generation == 2


def test_lock_released_when_runner_raises(fake_nfs):
    service, manifest, _ = _write_env(fake_nfs)
    lock_path = _update_lock_path(fake_nfs, manifest)

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        EnvTransaction(service, manifest.id, runner=runner).run(["conda", "install", "x"])

    assert not lock_path.exists()


# --- commit-on-success / preserve-on-failure --------------------------------


def test_success_path_commits_new_generation(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_env(fake_nfs)
    lock_path = _update_lock_path(fake_nfs, manifest)
    sanity_called = []

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        target = ctx.active_upper / "lib" / "numpy.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("installed")
        return CommandResult(returncode=0)

    def sanity(ctx: EnvUpdateContext) -> CommandResult:
        sanity_called.append(ctx.env_id)
        return CommandResult(returncode=0)

    result = EnvTransaction(
        service, manifest.id, runner=runner, sanity_check=sanity
    ).run(["conda", "install", "numpy"])

    assert result.status == "committed"
    assert result.committed is True
    assert result.overlay_preserved is False
    assert result.generation == 3
    assert result.sanity_ok is True
    assert sanity_called == [manifest.id]
    persisted = load_manifest(manifest_path)
    assert persisted.generation == 3
    assert persisted.state == "clean"
    assert len(persisted.pack_stack.lowers) == 2
    assert not lock_path.exists()


def test_command_failure_preserves_dirty_overlay_and_skips_commit(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_env(fake_nfs)
    lock_path = _update_lock_path(fake_nfs, manifest)
    sanity_called = []

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        (ctx.active_upper / "partial.pkg").write_text("half-written")
        return CommandResult(returncode=1, stderr="conda: solving failed")

    def sanity(ctx: EnvUpdateContext) -> CommandResult:
        sanity_called.append(ctx.env_id)
        return CommandResult(returncode=0)

    result = EnvTransaction(
        service, manifest.id, runner=runner, sanity_check=sanity
    ).run(["conda", "install", "does-not-exist"])

    assert result.status == "command_failed"
    assert result.committed is False
    assert result.overlay_preserved is True
    assert result.command_returncode == 1
    assert sanity_called == []  # sanity not run after a failed command
    upper = service.overlay_paths(manifest).active_upper
    assert (upper / "partial.pkg").exists()  # preserved for inspection
    assert load_manifest(manifest_path).generation == 2
    assert not lock_path.exists()


def test_sanity_failure_preserves_dirty_overlay_and_skips_commit(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_env(fake_nfs)
    lock_path = _update_lock_path(fake_nfs, manifest)

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        (ctx.active_upper / "broken.py").write_text("import boom")
        return CommandResult(returncode=0)

    def sanity(ctx: EnvUpdateContext) -> CommandResult:
        return CommandResult(returncode=1, stderr="ImportError: numpy")

    result = EnvTransaction(
        service, manifest.id, runner=runner, sanity_check=sanity
    ).run(["conda", "install", "numpy"])

    assert result.status == "sanity_failed"
    assert result.committed is False
    assert result.overlay_preserved is True
    assert result.command_returncode == 0
    assert result.sanity_ok is False
    upper = service.overlay_paths(manifest).active_upper
    assert (upper / "broken.py").exists()
    assert load_manifest(manifest_path).generation == 2
    assert not lock_path.exists()


# --- env status (read-only import path vs update mode) ----------------------


def test_env_status_clean_env_is_read_only_no_overlay(fake_nfs):
    service, manifest, _ = _write_env(fake_nfs)

    status = env_status(service, manifest.id)

    assert status["mode"] == "read-only"
    assert status["read_only"] is True
    assert status["overlay"] == "none"
    assert status["dirty"] is False
    assert status["generation"] == 2


def test_env_status_dirty_overlay_is_update_mode(fake_nfs):
    service, manifest, _ = _write_env(fake_nfs)
    upper = service.overlay_paths(manifest).active_upper
    upper.mkdir(parents=True, exist_ok=True)
    (upper / "staged.py").write_text("x")

    status = env_status(service, manifest.id)

    assert status["mode"] == "update"
    assert status["read_only"] is False
    assert status["overlay"] == "dirty"
    assert status["dirty"] is True


# --- CLI wrapper (fake runner) ----------------------------------------------


def test_cli_env_txn_passes_argv_to_runner(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_env(fake_nfs)
    captured = {}

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        captured["argv"] = list(ctx.argv)
        (ctx.active_upper / "x.py").write_text("x")
        return CommandResult(returncode=0)

    result = env_cli.run_env_txn(
        manifest.id, ["conda", "install", "numpy"], service=service, runner=runner
    )

    assert result["status"] == "committed"
    assert captured["argv"] == ["conda", "install", "numpy"]
    assert load_manifest(manifest_path).generation == 3


def test_cli_env_txn_pip_editable_passthrough_commits(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_env(fake_nfs)
    captured = {}

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        captured["argv"] = list(ctx.argv)
        # pip -e writes an egg-link / editable marker into site-packages.
        site = ctx.active_upper / "lib" / "python3.11" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        (site / "mypkg.egg-link").write_text("/path/to/mypkg\n.")
        return CommandResult(returncode=0)

    result = env_cli.run_env_txn(
        manifest.id, ["pip", "install", "-e", "./mypkg"], service=service, runner=runner
    )

    assert result["status"] == "committed"
    assert captured["argv"] == ["pip", "install", "-e", "./mypkg"]
    assert load_manifest(manifest_path).generation == 3


def test_cli_env_txn_command_strips_leading_double_dash(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, _ = _write_env(fake_nfs)
    captured = {}

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        captured["argv"] = list(ctx.argv)
        (ctx.active_upper / "x.py").write_text("x")
        return CommandResult(returncode=0)

    ns = argparse.Namespace(
        path=manifest.id, command=["--", "pip", "install", "numpy"], json=False
    )
    rc = env_cli.env_txn_command(ns, service=service, runner=runner)

    assert rc == 0
    assert captured["argv"] == ["pip", "install", "numpy"]


def test_cli_env_txn_failure_returns_nonzero(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, _ = _write_env(fake_nfs)

    def runner(ctx: EnvUpdateContext) -> CommandResult:
        return CommandResult(returncode=2, stderr="boom")

    ns = argparse.Namespace(path=manifest.id, command=["conda", "install", "x"], json=False)
    rc = env_cli.env_txn_command(ns, service=service, runner=runner)

    assert rc == 1


def test_cli_env_txn_empty_command_errors(fake_nfs):
    service, manifest, _ = _write_env(fake_nfs)
    ns = argparse.Namespace(path=manifest.id, command=[], json=False)

    rc = env_cli.env_txn_command(ns, service=service, runner=lambda ctx: CommandResult(0))

    assert rc == 2


def test_cli_env_status_command_runs(capsys, fake_nfs):
    service, manifest, _ = _write_env(fake_nfs)
    ns = argparse.Namespace(path=manifest.id, json=True)

    rc = env_cli.env_status_command(ns, service=service)

    assert rc == 0
    assert "read-only" in capsys.readouterr().out
