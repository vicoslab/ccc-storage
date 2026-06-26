"""`ccc-pack` entry-point stub (phase-00).

Prints the planned command surface and exits cleanly. The real implementation
(build/delta/verify/mount/extract/bundle/info) lands in phase-01+.
"""

from __future__ import annotations

import argparse

from ccc_layered_pack import __version__

_PLANNED = """\
planned commands (phase-01+):
  ccc-pack build   <src> <out.sqfs> [--exclude-boundary PATH ...] [--comp zstd] [--block 1M]
  ccc-pack delta   <src> --base <base.sqfs> <out-delta.sqfs> [--tombstone PATH ...]
  ccc-pack verify  <out.sqfs> --sha256 <hex> --size <bytes> [--file-count N]
  ccc-pack mount   <out.sqfs> <mountpoint> [--kernel]
  ccc-pack umount  <mountpoint>
  ccc-pack extract <out.sqfs> <dest> [--subpath P]
  ccc-pack bundle  build|unpack ...
  ccc-pack info    <out.sqfs>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-pack",
        description="Build/verify/read SquashFS packs (not yet implemented).",
    )
    parser.add_argument("--version", action="version", version=f"ccc-pack {__version__}")
    parser.add_argument("command", nargs="?", help="planned subcommand (not yet implemented)")
    parser.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    parser.parse_args(argv)

    print("ccc-pack: not yet implemented (phase-01).")
    print(_PLANNED)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
