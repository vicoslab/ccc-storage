from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "dev" / "validation" / "docker" / "nested-runtime-smoke.sh"


def test_nested_runtime_smoke_exists_and_validates_required_invariants():
    assert SCRIPT.exists()
    text = SCRIPT.read_text()
    for phrase in (
        "build_pack(parent_src, parent_pack_path, exclude_boundaries=[boundary_path])",
        "build_pack(child_src, child_pack_path)",
        "run --rm -i",
        "unsquashfs",
        "child payload leaked into parent pack",
        "ccc-storage mount-tree",
        "parent-only.txt",
        "bin/python",
        "nested SquashFS mount exposed parent and child data",
    ):
        assert phrase in text
