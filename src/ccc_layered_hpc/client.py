"""`ccc-layered-hpc` entry-point stub (phase-00).

The HPC-side client (stage a packset bundle from S3, FUSE-mount it, status,
push an output delta back to the import queue) and the CCC-side `ccc-tools hpc
run` orchestration arrive in phase-08. For now this prints the planned surface
and exits cleanly.
"""

from __future__ import annotations

import argparse

from ccc_layered_hpc import __version__

_PLANNED = """\
planned commands (phase-08):
  # HPC side (standalone)
  ccc-layered-hpc mount  <name>            # stage bundle from S3 + FUSE mount
  ccc-layered-hpc status <name>
  ccc-layered-hpc push   <name> [--message M]
  # CCC side (via ccc-tools)
  ccc-tools hpc run --site S --input PATH... --output PATH -- <submit cmd>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-layered-hpc",
        description="External-HPC packset client (not yet implemented).",
    )
    parser.add_argument(
        "--version", action="version", version=f"ccc-layered-hpc {__version__}"
    )
    parser.add_argument("command", nargs="?", help="planned subcommand (not yet implemented)")
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    parser.parse_args(argv)

    print("ccc-layered-hpc: not yet implemented (phase-08).")
    print(_PLANNED)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
