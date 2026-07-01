from __future__ import annotations

import json
from pathlib import Path

from ccc_storage_bench.perf import Workload, make_payload, relative_paths, run_write_read


def test_relative_paths_are_image_like_and_bounded_by_fanout():
    paths = relative_paths(5, fanout=3, prefix="sample", suffix=".jpg")

    assert paths == [
        Path("class_000") / "sample_000000.jpg",
        Path("class_001") / "sample_000001.jpg",
        Path("class_002") / "sample_000002.jpg",
        Path("class_000") / "sample_000003.jpg",
        Path("class_001") / "sample_000004.jpg",
    ]


def test_make_payload_is_deterministic_and_image_like():
    first = make_payload(index=7, size=1024, seed=123)
    second = make_payload(index=7, size=1024, seed=123)
    other = make_payload(index=8, size=1024, seed=123)

    assert first == second
    assert first != other
    assert len(first) == 1024
    assert first.startswith(b"\xff\xd8")
    assert first.endswith(b"\xff\xd9")


def test_run_write_read_returns_serializable_metrics(tmp_path):
    workload = Workload(
        name="tiny-images",
        files=4,
        size_bytes=2048,
        fanout=2,
        prefix="img",
        suffix=".jpg",
        seed=99,
    )

    result = run_write_read(
        tmp_path / "target",
        workload,
        target="direct-local",
        sync_after_write=False,
    )

    assert result["target"] == "direct-local"
    assert result["workload"]["name"] == "tiny-images"
    assert result["write"]["files"] == 4
    assert result["write"]["bytes"] == 8192
    assert result["read"]["files"] == 4
    assert result["read"]["bytes"] == 8192
    assert len(result["read"]["sha256"]) == 64
    assert result["write"]["files_per_second"] > 0
    assert result["read"]["mib_per_second"] > 0
    json.dumps(result, sort_keys=True)
