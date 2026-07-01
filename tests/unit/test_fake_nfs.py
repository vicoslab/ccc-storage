"""Fake-NFS layout, subdir access, and teardown."""

from __future__ import annotations

import pytest

from tests.fakes import fake_nfs as fake_nfs_mod


def test_create_lays_out_five_subdirs(test_root) -> None:
    nfs = fake_nfs_mod.create_fake_nfs(test_root)
    try:
        assert nfs.ccc_storage.name == ".ccc-storage"
        assert nfs.ccc_storage.is_dir()
        for name in fake_nfs_mod.SUBDIRS:
            assert nfs.subdir(name).is_dir()
        # Exactly the five authoritative subdirs, nothing extra.
        present = {p.name for p in nfs.ccc_storage.iterdir()}
        assert present == set(fake_nfs_mod.SUBDIRS)
    finally:
        nfs.cleanup()


def test_subdir_rejects_unknown(test_root) -> None:
    nfs = fake_nfs_mod.create_fake_nfs(test_root)
    try:
        with pytest.raises(KeyError):
            nfs.subdir("not-a-real-subdir")
    finally:
        nfs.cleanup()


def test_cleanup_removes_tree(test_root) -> None:
    nfs = fake_nfs_mod.create_fake_nfs(test_root)
    assert nfs.root.exists()
    nfs.cleanup()
    assert not nfs.root.exists()


def test_cleanup_is_idempotent(test_root) -> None:
    nfs = fake_nfs_mod.create_fake_nfs(test_root)
    nfs.cleanup()
    nfs.cleanup()  # second call must not raise


def test_distinct_trees_are_unique(test_root) -> None:
    a = fake_nfs_mod.create_fake_nfs(test_root)
    b = fake_nfs_mod.create_fake_nfs(test_root)
    try:
        assert a.root != b.root
    finally:
        a.cleanup()
        b.cleanup()


def test_fixture_sets_nfs_root_env(fake_nfs, monkeypatch) -> None:
    import os

    assert os.environ["CCC_NFS_ROOT"] == str(fake_nfs.ccc_storage)
    assert fake_nfs.ccc_storage.is_dir()
