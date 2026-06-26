"""`ccc-layered-mountd` entry-point stub (phase-00).

The real daemon (parent dispatcher FUSE, child mounts, overlay assembly,
workers, control socket) arrives in phase-02+. For now this provides a minimal
``--probe`` that reports the runtime ingredients the daemon will depend on, so
operators can sanity-check a node without the full stack. It does no mounting
and starts no socket.
"""

from __future__ import annotations

import argparse
import os
import shutil

from ccc_layered_mountd import __version__

# Binaries/devices the daemon needs at runtime; reported by --probe. This is a
# lightweight, dependency-free check (the authoritative, active probe lives in
# the test harness at tests/fakes/capability.py).
_RUNTIME_BINARIES = ("mksquashfs", "unsquashfs", "squashfuse", "fuse-overlayfs", "fusermount3")


def _probe_summary() -> list[str]:
    lines = ["ccc-layered-mountd runtime probe (lightweight):"]
    dev_fuse = os.path.exists("/dev/fuse") and os.access("/dev/fuse", os.R_OK | os.W_OK)
    lines.append(f"  /dev/fuse rw      : {'yes' if dev_fuse else 'no'}")
    for name in _RUNTIME_BINARIES:
        path = shutil.which(name)
        lines.append(f"  {name:<16}: {path or 'MISSING'}")
    lines.append("note: for the authoritative active probe run `make probe`.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-layered-mountd",
        description="Per-node layered-storage daemon (not yet implemented).",
    )
    parser.add_argument(
        "--version", action="version", version=f"ccc-layered-mountd {__version__}"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="print a lightweight runtime-ingredient summary and exit",
    )
    ns = parser.parse_args(argv)

    if ns.probe:
        print("\n".join(_probe_summary()))
        return 0

    print("ccc-layered-mountd: not yet implemented (phase-02).")
    print("It will own the parent dispatcher FUSE, child mounts, overlays,")
    print("the auto-commit/compaction workers, and the control socket.")
    print("Run with --probe to check runtime ingredients on this node.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
