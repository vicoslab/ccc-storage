from __future__ import annotations

from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import (
    ChildManifest,
    PackInfo,
    PackStack,
    dump_atomic,
    load_manifest,
)
from ccc_storage_mountd import daemon
from ccc_storage_mountd.daemon import MountdService, _safe_child_name
from ccc_storage_mountd.workers.auto_commit import AutoCommitWorker
from ccc_storage_mountd.workers.policy import CommitPolicy
from ccc_storage_pack.builder import BuildResult


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


def _write_child(fake_nfs, *, commit_mode="auto"):
    pack = fake_nfs.subdir("packs") / "base.sqfs"
    pack.write_bytes(b"base")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=3,
        commit_mode=commit_mode,
        pack_stack=PackStack(lowers=(PackInfo(path=str(pack), sha256="a" * 64, size=4),)),
    )
    manifest_path = fake_nfs.subdir("registry") / "foo.toml"
    dump_atomic(manifest_path, manifest)
    service = MountdService(nfs_root=fake_nfs.ccc_storage, run_dir=fake_nfs.root / "run")
    service.reload_registry()
    upper = service.overlay_paths(manifest).active_upper
    upper.mkdir(parents=True, exist_ok=True)
    (upper / "new.txt").write_text("new")
    return service, manifest, manifest_path


# A policy that triggers on any dirty bytes with no quiet-period gate.
_EAGER = CommitPolicy(max_dirty_bytes=1, min_quiet_seconds=0.0)


def test_tick_commits_when_policy_triggers(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_child(fake_nfs)
    worker = AutoCommitWorker(service, policy=_EAGER)

    summary = worker.tick()

    assert summary["decisions"]["dataset:foo"] == "trigger"
    assert summary["committed"] == ["dataset:foo"]
    persisted = load_manifest(manifest_path)
    assert persisted.generation == 4
    assert persisted.state == "clean"
    assert len(persisted.pack_stack.lowers) == 2


def test_tick_does_not_commit_clean_child(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_child(fake_nfs)
    # Remove the dirty file so the overlay is clean.
    (service.overlay_paths(manifest).active_upper / "new.txt").unlink()
    worker = AutoCommitWorker(service, policy=_EAGER)

    summary = worker.tick()

    assert summary["decisions"]["dataset:foo"] == "noop"
    assert summary["committed"] == []
    assert load_manifest(manifest_path).generation == 3


def test_tick_respects_manual_only_policy(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, _manifest, manifest_path = _write_child(fake_nfs, commit_mode="manual")
    worker = AutoCommitWorker(service, policy=_EAGER)

    summary = worker.tick()

    assert summary["decisions"]["dataset:foo"] == "manual"
    assert summary["committed"] == []
    assert load_manifest(manifest_path).generation == 3


def test_tick_skips_gracefully_when_commit_lock_held(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_child(fake_nfs)
    # Simulate a concurrent manual commit holding the per-child commit lock.
    lock_path = fake_nfs.ccc_storage / "locks" / f"{_safe_child_name(manifest.id)}.commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{}")
    worker = AutoCommitWorker(service, policy=_EAGER)

    summary = worker.tick()

    assert summary["decisions"]["dataset:foo"] == "trigger"
    assert summary["committed"] == []
    assert summary["skipped"] == [{"id": "dataset:foo", "reason": "locked"}]
    assert load_manifest(manifest_path).generation == 3


def test_poke_commits_single_child_when_triggered(monkeypatch, fake_nfs):
    monkeypatch.setattr(daemon, "build_delta", _fake_build_delta)
    service, manifest, manifest_path = _write_child(fake_nfs)
    worker = AutoCommitWorker(service, policy=_EAGER)

    result = worker.poke(manifest.id)

    assert result["decision"] == "trigger"
    assert result["committed"] is True
    assert load_manifest(manifest_path).generation == 4
