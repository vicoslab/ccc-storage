"""`ccc-layered` user/container CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
from pathlib import Path
from typing import Any

from ccc_layered_cli import __version__
from ccc_layered_cli import env as env_cli
from ccc_layered_core.protocol import Request, decode_response, encode_request

DEFAULT_SOCKET = "/run/ccc-layered/mountd.sock"

_NOT_IMPLEMENTED = {
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


def _request(
    command: str,
    *,
    path: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    sock_path = _socket_path()
    if not Path(sock_path).exists():
        return 2, {"error": f"mountd not reachable on this node: {sock_path}", "code": "ENOSOCK"}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect(sock_path)
        sock.sendall(encode_request(Request(command=command, path=path, payload=payload or {})))
        response = decode_response(_read_line(sock))
    except OSError as exc:
        return 2, {"error": f"mountd not reachable on this node: {exc}", "code": "ENOSOCK"}
    finally:
        sock.close()
    if not response.ok:
        return 2, {"error": response.error, "code": response.code}
    return 0, response.result


def _read_line(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks)


def _print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if "children" in result:
        for child in result["children"]:
            if isinstance(child, str):  # managed-parent name listing
                print(child)
                continue
            mounted = "mounted" if child.get("mounted") else "not-mounted"
            print(f"{child['id']}\tgen={child['generation']}\t{mounted}")
        return
    for key, value in result.items():
        print(f"{key}: {value}")


def _doctor(as_json: bool = False) -> int:
    sock_path = _socket_path()
    if _socket_reachable(sock_path):
        code, result = _request("doctor")
        _print_result(result, as_json=as_json)
        return code

    nfs_root = os.environ.get("CCC_NFS_ROOT")
    dev_fuse = os.path.exists("/dev/fuse") and os.access("/dev/fuse", os.R_OK | os.W_OK)
    fusermount = bool(shutil.which("fusermount3"))
    result = {
        "mountd_socket": "down",
        "socket_path": sock_path,
        "nfs_root": nfs_root or "",
        "nfs_root_reachable": bool(nfs_root and Path(nfs_root).is_dir()),
        "dev_fuse_rw": dev_fuse,
        "fusermount3": fusermount,
    }
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("ccc-layered doctor:")
        print(f"  mountd socket     : DOWN ({sock_path}) — mountd not reachable on this node")
        if nfs_root:
            state = "reachable" if result["nfs_root_reachable"] else "MISSING"
            print(f"  NFS root          : {state} ({nfs_root})")
        else:
            print("  NFS root          : unset (set $CCC_NFS_ROOT)")
        print(f"  /dev/fuse rw      : {'yes' if dev_fuse else 'no'}")
        print(f"  fusermount3       : {'present' if fusermount else 'MISSING'}")
    return 0


def _socket_command(ns: argparse.Namespace) -> int:
    payload: dict[str, Any] = {}
    if hasattr(ns, "message"):
        payload["message"] = ns.message
    if hasattr(ns, "to"):
        payload["to"] = ns.to
    if ns.cmd == "pin":
        payload["pinned"] = not getattr(ns, "clear", False)
    code, result = _request(ns.cmd, path=getattr(ns, "path", ""), payload=payload)
    if code != 0:
        if getattr(ns, "json", False):
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(result["error"])
        return code
    _print_result(result, as_json=getattr(ns, "json", False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc-layered",
        description="Unprivileged layered-storage control CLI.",
    )
    parser.add_argument("--version", action="version", version=f"ccc-layered {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    doctor = sub.add_parser("doctor", help="probe socket, NFS root, and local capabilities")
    doctor.add_argument("--json", action="store_true")

    for name in ("status", "mount", "mount-tree", "umount"):
        p = sub.add_parser(name, help=f"{name} a managed child via mountd")
        p.add_argument("path")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=_socket_command)

    commit = sub.add_parser("commit", help="commit a dirty shared overlay via mountd")
    commit.add_argument("path")
    commit.add_argument("-m", "--message", default="")
    commit.add_argument("--json", action="store_true")
    commit.set_defaults(func=_socket_command)

    pin = sub.add_parser("pin", help="pin/unpin a child to exempt it from cold-tier GC")
    pin.add_argument("path")
    pin.add_argument(
        "--clear",
        "--unset",
        dest="clear",
        action="store_true",
        help="clear the pin instead of setting it",
    )
    pin.add_argument("--json", action="store_true")
    pin.set_defaults(func=_socket_command)

    list_cmd = sub.add_parser("ls", help="list managed children via mountd")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=_socket_command)

    # --- managed-parent namespace ops (phase-04) ----------------------------
    parent_ls = sub.add_parser("parent-ls", help="list managed-parent child names")
    parent_ls.add_argument("--json", action="store_true")
    parent_ls.set_defaults(func=_socket_command)

    for name in ("create", "rmdir", "access"):
        p = sub.add_parser(name, help=f"{name} a managed-parent child via mountd")
        p.add_argument("path", help="child name")
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=_socket_command)

    rename = sub.add_parser("rename", help="rename a managed-parent child via mountd")
    rename.add_argument("path", help="current child name")
    rename.add_argument("to", help="new child name")
    rename.add_argument("--json", action="store_true")
    rename.set_defaults(func=_socket_command)

    # --- managed conda env transactions (phase-07) --------------------------
    env_cli.add_parsers(sub)

    for name in _NOT_IMPLEMENTED:
        sub.add_parser(name, help=f"not yet implemented ({_NOT_IMPLEMENTED[name]})")

    ns, _rest = parser.parse_known_args(argv)

    if ns.cmd == "doctor":
        return _doctor(as_json=ns.json)
    if hasattr(ns, "func"):
        return ns.func(ns)
    if ns.cmd in _NOT_IMPLEMENTED:
        print(f"ccc-layered {ns.cmd}: not yet implemented ({_NOT_IMPLEMENTED[ns.cmd]}).")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
