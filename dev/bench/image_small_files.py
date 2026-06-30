#!/usr/bin/env python3
"""Benchmark image-like small files across SSD, NFS, SquashFS, and dirty overlay.

Run inside a FUSE-capable container when measuring SquashFS/fuse-overlayfs:

  python dev/bench/image_small_files.py --ssd-root /dev/bench/ssd --nfs-root /dev/bench/nfs

The synthetic files are JPEG-like incompressible payloads. The benchmark reads
all bytes and computes a checksum so reads cannot be optimized away.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def _timed(fn: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


def _sync() -> float:
    _, elapsed = _timed(os.sync)
    return elapsed


def _payloads(files: int, size: int, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    payloads: list[bytes] = []
    for _idx in range(files):
        body_size = max(size - 4, 1)
        body = rng.randbytes(body_size)
        # JPEG SOI + pseudo-random body + EOI.  Files remain image-like and mostly
        # incompressible, like already-compressed dataset images.
        payloads.append(b"\xff\xd8" + body + b"\xff\xd9")
    return payloads


def _relative_paths(files: int) -> list[Path]:
    return [Path(f"class_{idx % 100:03d}") / f"img_{idx:06d}.jpg" for idx in range(files)]


def _write_files(root: Path, rels: list[Path], payloads: list[bytes]) -> dict[str, Any]:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    for rel, payload in zip(rels, payloads, strict=True):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return {"files": len(rels), "bytes": sum(len(item) for item in payloads)}


def _read_files(root: Path, rels: list[Path], *, chunk_size: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    total = 0
    for rel in rels:
        with (root / rel).open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
    return {"files": len(rels), "bytes": total, "sha256": digest.hexdigest()}


def _metric(name: str, seconds: float, result: dict[str, Any]) -> dict[str, Any]:
    bytes_read = int(result.get("bytes", 0))
    files = int(result.get("files", 0))
    return {
        "name": name,
        "seconds": round(seconds, 6),
        "files": files,
        "bytes": bytes_read,
        "files_per_second": round(files / seconds, 2) if seconds else None,
        "mib_per_second": round(bytes_read / (1024 * 1024) / seconds, 2) if seconds else None,
        **{k: v for k, v in result.items() if k not in {"files", "bytes"}},
    }


def _wait_mount(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _run(["findmnt", "-T", str(path)], check=False).returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError(f"mount did not appear: {path}")


def _umount(path: Path) -> None:
    if path.exists():
        _run(["fusermount3", "-u", str(path)], check=False)
        _run(["umount", str(path)], check=False)


def _build_squashfs(src: Path, pack: Path) -> dict[str, Any]:
    if pack.exists():
        pack.unlink()
    pack.parent.mkdir(parents=True, exist_ok=True)
    cp = _run([
        "mksquashfs",
        str(src),
        str(pack),
        "-noappend",
        "-no-progress",
        "-comp",
        "zstd",
        "-b",
        "1M",
    ])
    return {
        "pack_bytes": pack.stat().st_size,
        "stdout_tail": cp.stdout[-400:],
        "stderr_tail": cp.stderr[-400:],
    }


def _mount_squashfs(pack: Path, mountpoint: Path) -> None:
    _umount(mountpoint)
    if mountpoint.exists():
        shutil.rmtree(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)
    _run(["squashfuse", "-o", "ro", str(pack), str(mountpoint)])
    _wait_mount(mountpoint)


def _mount_overlay(lower: Path, upper: Path, work: Path, mountpoint: Path) -> None:
    _umount(mountpoint)
    for path in (upper, work):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
    if mountpoint.exists():
        shutil.rmtree(mountpoint)
    mountpoint.mkdir(parents=True, exist_ok=True)
    _run([
        "fuse-overlayfs",
        "-o",
        f"lowerdir={lower},upperdir={upper},workdir={work}",
        str(mountpoint),
    ])
    _wait_mount(mountpoint)


def _host_info(paths: list[Path]) -> dict[str, Any]:
    info: dict[str, Any] = {"uname": os.uname().sysname + " " + os.uname().release}
    mounts = {}
    for path in paths:
        cp = _run(
            ["findmnt", "-T", str(path), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS", "-n"],
            check=False,
        )
        mounts[str(path)] = cp.stdout.strip()
    info["mounts"] = mounts
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssd-root", type=Path, required=True)
    parser.add_argument("--nfs-root", type=Path, required=True)
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--size-kib", type=int, default=32)
    parser.add_argument("--seed", type=int, default=20260628)
    parser.add_argument("--chunk-kib", type=int, default=256)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    ns = parser.parse_args(argv)

    size = ns.size_kib * 1024
    chunk_size = ns.chunk_kib * 1024
    rels = _relative_paths(ns.files)
    payloads, payload_gen_s = _timed(lambda: _payloads(ns.files, size, ns.seed))
    payload_info = {"files": ns.files, "bytes": sum(len(item) for item in payloads)}

    for root in (ns.ssd_root, ns.nfs_root):
        root.mkdir(parents=True, exist_ok=True)

    ssd_direct = ns.ssd_root / "direct-images"
    nfs_direct = ns.nfs_root / "direct-images"
    pack = ns.nfs_root / "packs" / "images.sqfs"
    sqfs_mount = ns.nfs_root / "mounts" / "squashfs-ro"
    overlay_mount = ns.nfs_root / "mounts" / "overlay-rw"
    overlay_upper = ns.nfs_root / "overlay" / "upper"
    overlay_work = ns.nfs_root / "overlay" / "work"

    metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        result, seconds = _timed(lambda: _write_files(ssd_direct, rels, payloads))
        sync_s = _sync()
        result["sync_seconds"] = round(sync_s, 6)
        metrics.append(_metric("write_direct_ssd", seconds + sync_s, result))

        result, seconds = _timed(lambda: _write_files(nfs_direct, rels, payloads))
        sync_s = _sync()
        result["sync_seconds"] = round(sync_s, 6)
        metrics.append(_metric("write_direct_nfs", seconds + sync_s, result))

        result, seconds = _timed(lambda: _read_files(ssd_direct, rels, chunk_size=chunk_size))
        metrics.append(_metric("read_direct_ssd", seconds, result))

        result, seconds = _timed(lambda: _read_files(nfs_direct, rels, chunk_size=chunk_size))
        metrics.append(_metric("read_direct_nfs", seconds, result))

        build_result, build_s = _timed(lambda: _build_squashfs(nfs_direct, pack))
        metrics.append(
            _metric(
                "build_squashfs_from_nfs",
                build_s,
                {"files": ns.files, "bytes": payload_info["bytes"], **build_result},
            )
        )

        _mount_squashfs(pack, sqfs_mount)
        result, seconds = _timed(lambda: _read_files(sqfs_mount, rels, chunk_size=chunk_size))
        metrics.append(_metric("read_squashfs_pack_on_nfs", seconds, result))

        _mount_overlay(sqfs_mount, overlay_upper, overlay_work, overlay_mount)
        result, seconds = _timed(lambda: _read_files(overlay_mount, rels, chunk_size=chunk_size))
        metrics.append(_metric("read_overlay_lower_squashfs", seconds, result))

        dirty_rels = [Path("dirty") / rel for rel in rels]
        result, seconds = _timed(lambda: _write_files(overlay_mount / "dirty", rels, payloads))
        sync_s = _sync()
        result["sync_seconds"] = round(sync_s, 6)
        metrics.append(_metric("write_dirty_overlay_upper_nfs", seconds + sync_s, result))

        result, seconds = _timed(
            lambda: _read_files(overlay_mount, dirty_rels, chunk_size=chunk_size)
        )
        metrics.append(_metric("read_dirty_overlay_upper_nfs", seconds, result))
    except Exception as exc:  # keep partial results useful
        errors.append(repr(exc))
        raise
    finally:
        _umount(overlay_mount)
        _umount(sqfs_mount)

    by_name = {item["name"]: item for item in metrics}
    comparisons = {}
    baseline_read_ssd = by_name["read_direct_ssd"]["files_per_second"]
    baseline_read_nfs = by_name["read_direct_nfs"]["files_per_second"]
    for name in (
        "read_direct_nfs",
        "read_squashfs_pack_on_nfs",
        "read_overlay_lower_squashfs",
        "read_dirty_overlay_upper_nfs",
    ):
        fps = by_name[name]["files_per_second"]
        comparisons[f"{name}_vs_ssd_files_per_second"] = round(fps / baseline_read_ssd, 3)
        comparisons[f"{name}_vs_nfs_files_per_second"] = round(fps / baseline_read_nfs, 3)

    output = {
        "parameters": {
            "files": ns.files,
            "size_kib": ns.size_kib,
            "total_mib": round(payload_info["bytes"] / 1024 / 1024, 3),
            "seed": ns.seed,
            "chunk_kib": ns.chunk_kib,
        },
        "payload_generation_seconds": round(payload_gen_s, 6),
        "host": _host_info([ns.ssd_root, ns.nfs_root]),
        "metrics": metrics,
        "comparisons": comparisons,
        "errors": errors,
    }

    text = json.dumps(output, indent=2, sort_keys=True)
    print(text)
    if ns.json_out:
        ns.json_out.parent.mkdir(parents=True, exist_ok=True)
        ns.json_out.write_text(text + "\n")
    if not ns.keep:
        cleanup_paths = (
            ssd_direct,
            nfs_direct,
            pack.parent,
            ns.nfs_root / "mounts",
            ns.nfs_root / "overlay",
        )
        for path in cleanup_paths:
            shutil.rmtree(path, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
