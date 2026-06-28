#!/usr/bin/env python3
"""Synthetic conda-env small-file benchmark for CCC layered storage.

This is intentionally a local, reproducible smoke benchmark rather than a CI
threshold test.  Use it to compare raw small-file metadata traversal against
committing the same tree into one SquashFS object.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ccc_layered_pack.builder import build_pack  # noqa: E402


def _write_tree(root: Path, files: int, payload_bytes: int) -> None:
    payload = ("x" * max(payload_bytes - 1, 1) + "\n").encode()
    layouts = (
        "lib/python3.11/site-packages/pkg{pkg}/module_{idx}.py",
        "lib/python3.11/site-packages/pkg{pkg}/data/file_{idx}.txt",
        "conda-meta/pkg{pkg}-{idx}.json",
        "include/pkg{pkg}/header_{idx}.h",
        "bin/tool_{idx}",
    )
    for idx in range(files):
        rel = layouts[idx % len(layouts)].format(pkg=idx % 257, idx=idx)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _stat_tree(root: Path) -> tuple[int, int]:
    count = 0
    size = 0
    for path in root.rglob("*"):
        if path.is_file():
            st = path.stat()
            count += 1
            size += st.st_size
    return count, size


def _time(fn):
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=3000)
    parser.add_argument("--payload-bytes", type=int, default=256)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT / ".scratch" / "bench" / "conda-small-files",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    ns = parser.parse_args(argv)

    if ns.root.exists():
        shutil.rmtree(ns.root)
    tree = ns.root / "env"
    pack = ns.root / "packs" / "env.sqfs"
    tree.mkdir(parents=True)

    _, create_s = _time(lambda: _write_tree(tree, ns.files, ns.payload_bytes))
    (file_count, raw_bytes), raw_stat_s = _time(lambda: _stat_tree(tree))
    result, build_s = _time(lambda: build_pack(tree, pack, comp="zstd", block="1M"))
    (_, pack_bytes), pack_stat_s = _time(lambda: _stat_tree(pack.parent))

    summary = {
        "files": file_count,
        "payload_bytes_per_file": ns.payload_bytes,
        "raw_bytes": raw_bytes,
        "pack_size": result.pack.size,
        "compression_ratio_raw_to_pack": round(raw_bytes / result.pack.size, 3)
        if result.pack.size
        else None,
        "create_seconds": round(create_s, 6),
        "raw_stat_seconds": round(raw_stat_s, 6),
        "squashfs_build_seconds": round(build_s, 6),
        "pack_stat_seconds": round(pack_stat_s, 6),
        "raw_files_per_second_stat": round(file_count / raw_stat_s, 1) if raw_stat_s else None,
        "pack_files_per_nfs_object": file_count,
        "pack_path": str(pack),
        "pack_sha256": result.pack.sha256,
        "pack_bytes_seen_by_stat": pack_bytes,
    }

    text = json.dumps(summary, indent=2, sort_keys=True)
    print(text)
    if ns.json_out:
        ns.json_out.parent.mkdir(parents=True, exist_ok=True)
        ns.json_out.write_text(text + "\n")
    if not ns.keep:
        # Keep the JSON but remove the synthetic input tree and pack payload.
        shutil.rmtree(ns.root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
