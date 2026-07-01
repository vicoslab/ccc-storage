"""Write/read performance benchmark helpers for CCC layered storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Workload:
    """Synthetic file workload shape.

    Payloads are deterministic and mostly incompressible so the benchmark behaves
    like JPEG/PNG-like image datasets rather than text fixtures.
    """

    name: str
    files: int
    size_bytes: int
    fanout: int = 100
    prefix: str = "img"
    suffix: str = ".jpg"
    seed: int = 20260630

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_bytes"] = self.files * self.size_bytes
        data["size_mib"] = self.size_bytes / (1024 * 1024)
        return data


def relative_paths(
    files: int,
    *,
    fanout: int = 100,
    prefix: str = "img",
    suffix: str = ".jpg",
) -> list[Path]:
    """Return deterministic image-dataset-like relative paths."""

    if files < 0:
        raise ValueError("files must be non-negative")
    if fanout <= 0:
        raise ValueError("fanout must be positive")
    return [
        Path(f"class_{idx % fanout:03d}") / f"{prefix}_{idx:06d}{suffix}"
        for idx in range(files)
    ]


def make_payload(*, index: int, size: int, seed: int) -> bytes:
    """Return deterministic pseudo-image bytes of exactly *size* bytes."""

    if size < 4:
        raise ValueError("payload size must be at least 4 bytes")
    rng = random.Random((seed << 32) ^ index)
    return b"\xff\xd8" + rng.randbytes(size - 4) + b"\xff\xd9"


def _sync() -> float:
    start = time.perf_counter()
    os.sync()
    return time.perf_counter() - start


def _metric(
    seconds: float,
    *,
    files: int,
    bytes_total: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metric = {
        "seconds": seconds,
        "files": files,
        "bytes": bytes_total,
        "files_per_second": files / seconds if seconds > 0 else None,
        "mib_per_second": (bytes_total / (1024 * 1024)) / seconds if seconds > 0 else None,
    }
    if extra:
        metric.update(extra)
    return metric


def _findmnt(path: Path) -> dict[str, str]:
    cp = subprocess.run(
        ["findmnt", "-T", str(path), "-o", "TARGET,SOURCE,FSTYPE", "-n"],
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        return {"target": "", "source": "", "fstype": ""}
    parts = cp.stdout.strip().split(maxsplit=2)
    while len(parts) < 3:
        parts.append("")
    return {"target": parts[0], "source": parts[1], "fstype": parts[2]}


def _write_workload(root: Path, workload: Workload, rels: list[Path]) -> dict[str, Any]:
    start = time.perf_counter()
    for idx, rel in enumerate(rels):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make_payload(index=idx, size=workload.size_bytes, seed=workload.seed))
    return {
        "seconds": time.perf_counter() - start,
        "files": workload.files,
        "bytes": workload.files * workload.size_bytes,
    }


def _read_workload(root: Path, rels: list[Path], *, chunk_size: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    total = 0
    start = time.perf_counter()
    for rel in rels:
        with (root / rel).open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
    return {
        "seconds": time.perf_counter() - start,
        "files": len(rels),
        "bytes": total,
        "sha256": digest.hexdigest(),
    }


def run_write_read(
    root: str | Path,
    workload: Workload,
    *,
    target: str,
    sync_after_write: bool = True,
    chunk_size: int = 8 * 1024 * 1024,
    clean: bool = True,
) -> dict[str, Any]:
    """Write then fully read *workload* under *root* and return JSON-safe metrics."""

    root_path = Path(root)
    if clean and root_path.exists():
        shutil.rmtree(root_path)
    root_path.mkdir(parents=True, exist_ok=True)
    rels = relative_paths(
        workload.files,
        fanout=workload.fanout,
        prefix=workload.prefix,
        suffix=workload.suffix,
    )
    mount_info = _findmnt(root_path)
    write_raw = _write_workload(root_path, workload, rels)
    sync_seconds = _sync() if sync_after_write else 0.0
    read_raw = _read_workload(root_path, rels, chunk_size=chunk_size)
    write_seconds = write_raw["seconds"] + sync_seconds
    return {
        "target": target,
        "root": str(root_path),
        "mount": mount_info,
        "workload": workload.to_dict(),
        "write": _metric(
            write_seconds,
            files=write_raw["files"],
            bytes_total=write_raw["bytes"],
            extra={
                "data_seconds": write_raw["seconds"],
                "sync_seconds": sync_seconds,
            },
        ),
        "read": _metric(
            read_raw["seconds"],
            files=read_raw["files"],
            bytes_total=read_raw["bytes"],
            extra={"sha256": read_raw["sha256"]},
        ),
    }


def _parse_size(ns: argparse.Namespace) -> int:
    provided = [
        ns.size_bytes is not None,
        ns.size_kib is not None,
        ns.size_mib is not None,
    ]
    if sum(provided) != 1:
        raise SystemExit("exactly one of --size-bytes, --size-kib, or --size-mib is required")
    if ns.size_bytes is not None:
        return int(ns.size_bytes)
    if ns.size_kib is not None:
        return int(ns.size_kib) * 1024
    return int(ns.size_mib) * 1024 * 1024


def main(argv: list[str] | None = None, *, prog: str = "ccc-storage benchmark") -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--workload-name", required=True)
    parser.add_argument("--files", type=int, required=True)
    parser.add_argument("--size-bytes", type=int)
    parser.add_argument("--size-kib", type=int)
    parser.add_argument("--size-mib", type=int)
    parser.add_argument("--fanout", type=int, default=100)
    parser.add_argument("--prefix", default="img")
    parser.add_argument("--suffix", default=".jpg")
    parser.add_argument("--seed", type=int, default=20260630)
    parser.add_argument("--chunk-mib", type=int, default=8)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--no-clean", action="store_true")
    ns = parser.parse_args(argv)
    workload = Workload(
        name=ns.workload_name,
        files=ns.files,
        size_bytes=_parse_size(ns),
        fanout=ns.fanout,
        prefix=ns.prefix,
        suffix=ns.suffix,
        seed=ns.seed,
    )
    result = run_write_read(
        ns.root,
        workload,
        target=ns.target,
        sync_after_write=not ns.no_sync,
        chunk_size=ns.chunk_mib * 1024 * 1024,
        clean=not ns.no_clean,
    )
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if ns.json_out:
        ns.json_out.parent.mkdir(parents=True, exist_ok=True)
        ns.json_out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
