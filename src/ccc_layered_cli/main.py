"""`ccc-layered` entry-point stub (phase-00).

Most subcommands (`status/commit/pin/import/hpc-export/ls/mount/umount`) talk to
the mountd control socket and are not implemented until phase-02+. `doctor` is
implemented now as an offline diagnostic: it reports whether the control socket
exists, whether the configured NFS root is reachable, and a couple of local
capability flags — with actionable, non-alarming messages and a clean exit.

This package imports only the standard library and ``ccc_layered_core``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
from pathlib import Path

from ccc_layered_cli import __version__

DEFAULT_SOCKET = "/run/ccc-layered/mountd.sock"

_NOT_IMPLEMENTED = {
    "status": "phase-02",
    "ls": "phase-02",
    "mount": "phase-04",
    "umount": "phase-04",
    "commit": "phase-03",
    "pin": "phase-05",
    "import": "phase-03",
    "hpc-export": "phase-08",
}


def _socket_path() -> str:
    return os.environ.get("CCC_MOUNTD_SOCK", DEFAULT_SOCKET)


def _socket_reachable(path: str) -> bool:
    if not Path(path).exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _doctor() -> int:
    sock_path = _socket_path()
    nfs_root = os.environ.get("CCC_NFS_ROOT")
    dev_fuse = os.path.exists("/dev/fuse") and os.access("/dev/fuse", os.R_OK | os.W_OK)
    fusermount = bool(shutil.which("fusermount3"))

    print("ccc-layered doctor:")
    if _socket_reachable(sock_path):
        print(f"  mountd socket     : UP   ({sock_path})")
    else:
        print(f"  mountd socket     : DOWN ({sock_path}) — mountd not reachable on this node")
    if nfs_root:
        reachable = Path(nfs_root).is_dir()
        state = "reachable" if reachable else "MISSING"
        print(f"  NFS root          : {state} ({nfs_root})")
    else:
        print("  NFS root          : unset (set $CCC_NFS_ROOT)")
    print(f"  /dev/fuse rw      : {'yes' if dev_fuse else 'no'}")
    print(f"  fusermount3       : {'present' if fusermount else 'MISSING'}")
    # doctor is a diagnostic, not a gate: it always exits 0 so it is safe to run
    # anywhere, including before a daemon exists.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-layered",
        description="Unprivileged layered-storage control CLI.",
    )
    parser.add_argument("--version", action="version", version=f"ccc-layered {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="probe socket, NFS root, and local capabilities (offline)")
    for name in _NOT_IMPLEMENTED:
        sub.add_parser(name, help=f"not yet implemented ({_NOT_IMPLEMENTED[name]})")

    ns, _rest = parser.parse_known_args(argv)

    if ns.cmd == "doctor":
        return _doctor()
    if ns.cmd in _NOT_IMPLEMENTED:
        print(f"ccc-layered {ns.cmd}: not yet implemented ({_NOT_IMPLEMENTED[ns.cmd]}).")
        print("It will be served by the mountd control socket.")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
