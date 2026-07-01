"""Node harness placeholder: start/stop N nodes cleanly, zero stray mounts."""

from __future__ import annotations

from tests.fakes import fake_nfs as fake_nfs_mod
from tests.fakes import node_harness


def test_cluster_starts_and_stops_three_nodes(test_root, tmp_path) -> None:
    nfs = fake_nfs_mod.create_fake_nfs(test_root)
    run_base = tmp_path / "run"
    pids: list[int] = []
    try:
        with node_harness.node_cluster(3, nfs.ccc_storage, base_run=run_base) as nodes:
            assert len(nodes) == 3
            for node in nodes:
                assert node.is_running()
                assert node.pid is not None
                assert node.run_dir.is_dir()
                pids.append(node.pid)
            assert len(set(pids)) == 3  # distinct processes
        # After the context exits every node is reaped.
        for node in nodes:
            assert not node.is_running()
    finally:
        nfs.cleanup()


def test_cluster_leaves_no_stray_mounts(test_root, tmp_path) -> None:
    nfs = fake_nfs_mod.create_fake_nfs(test_root)
    run_base = tmp_path / "run"
    try:
        with node_harness.node_cluster(2, nfs.ccc_storage, base_run=run_base):
            pass
        # The placeholder mounts nothing, so the post-teardown sweep is empty.
        assert node_harness.sweep_stray_mounts(run_base) == []
    finally:
        nfs.cleanup()


def test_node_start_stop_direct(test_root, tmp_path) -> None:
    node = node_harness.Node(
        name="solo",
        run_dir=tmp_path / "solo",
        nfs_root=test_root,
    )
    assert not node.is_running()
    assert node.pid is None
    node.start()
    try:
        assert node.is_running()
        assert node.pid is not None
    finally:
        node.stop()
    assert not node.is_running()
    assert node.proc is None


def test_node_stop_is_idempotent(tmp_path) -> None:
    node = node_harness.Node(name="solo", run_dir=tmp_path / "solo", nfs_root=tmp_path)
    node.stop()  # never started: must not raise
    node.start()
    node.stop()
    node.stop()  # double stop: must not raise


def test_sweep_on_clean_path_returns_empty(tmp_path) -> None:
    assert node_harness.sweep_stray_mounts(tmp_path) == []
