"""External-HPC staged packset client foundation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from ccc_storage_hpc import __version__
from ccc_storage_pack.bundle import MountGraph, MountGraphNode


class ExcludedChildError(RuntimeError):
    """Raised when a staged job touches a child excluded from the closure."""


@dataclass(frozen=True)
class IncludedPath:
    child_id: str
    path: str
    inner_path: str


class StagedPackset:
    """A lightweight mount-graph lookup model for staged HPC jobs.

    This is not a FUSE implementation. It is the deterministic core used by
    tests and future FUSE adapters to decide whether a path is included or an
    explicit excluded-child stub.
    """

    def __init__(self, graph: MountGraph) -> None:
        self.graph = graph

    def lookup(self, rel_path: str) -> IncludedPath:
        clean = rel_path.strip("/")
        for node in sorted(self.graph.excluded, key=lambda n: len(n.path), reverse=True):
            path = node.path.strip("/")
            if clean == path or clean.startswith(path + "/"):
                raise ExcludedChildError(
                    f"child {node.child_id} at {node.path} is excluded from this HPC closure: "
                    f"{node.reason or 'not included'}"
                )
        best: MountGraphNode | None = None
        for node in sorted(self.graph.included, key=lambda n: len(n.path), reverse=True):
            path = "" if node.path in ("", ".") else node.path.strip("/")
            if not path or clean == path or clean.startswith(path + "/"):
                best = node
                break
        if best is None:
            # The root node owns paths outside selected children.
            root = next((n for n in self.graph.included if n.child_id == self.graph.root), None)
            if root is None:
                raise FileNotFoundError(rel_path)
            best = root
        prefix = "" if best.path in ("", ".") else best.path.strip("/")
        inner = clean[len(prefix) :].lstrip("/") if prefix else clean
        return IncludedPath(child_id=best.child_id, path=clean, inner_path=inner)


def main(argv: list[str] | None = None, *, prog: str = "ccc-storage hpc") -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="External-HPC packset client foundation.",
    )
    parser.add_argument("--version", action="version", version=f"{prog} {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    status = sub.add_parser("status", help="show staged packset status")
    status.add_argument("name")
    mount = sub.add_parser(
        "mount", help="stage/mount a packset bundle (runtime FUSE adapter pending)"
    )
    mount.add_argument("name")
    push = sub.add_parser(
        "push", help="push output delta to import queue (runtime adapter pending)"
    )
    push.add_argument("name")
    push.add_argument("-m", "--message", default="")
    ns = parser.parse_args(argv)
    if ns.cmd == "status":
        print(f"{ns.name}: staged packset status available; FUSE mount adapter pending")
        return 0
    if ns.cmd in {"mount", "push"}:
        print(
            f"{prog} {ns.cmd}: runtime adapter not yet implemented; "
            "use library APIs in tests"
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
