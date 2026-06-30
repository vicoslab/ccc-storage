"""`ccc-storage pack` CLI for immutable SquashFS pack operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ccc_layered_core.manifest import ChildManifest, PackStack, dump_atomic, load_manifest
from ccc_layered_pack import __version__
from ccc_layered_pack.builder import PackBuildError, build_pack
from ccc_layered_pack.verify import VerificationError, verify_pack


def _cmd_build(ns: argparse.Namespace) -> int:
    try:
        result = build_pack(
            ns.src,
            ns.out,
            comp=ns.comp,
            block=ns.block,
            exclude_boundaries=ns.exclude_boundary,
        )
        if ns.manifest:
            child_id = ns.child_id or Path(ns.out).stem
            manifest = ChildManifest(
                id=child_id,
                name=ns.name or child_id,
                type=ns.type,
                generation=ns.generation,
                pack_stack=PackStack(active_revision=ns.revision, lowers=(result.pack,)),
            )
            dump_atomic(ns.manifest, manifest)
        print(
            json.dumps(
                {"pack": result.pack.to_dict(), "manifest": ns.manifest or ""},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except PackBuildError as exc:
        print(f"ccc-storage pack build: {exc}")
        return 2


def _cmd_verify(ns: argparse.Namespace) -> int:
    try:
        result = verify_pack(ns.pack, sha256=ns.sha256, size=ns.size, file_count=ns.file_count)
        print(json.dumps(result.__dict__, indent=2, sort_keys=True))
        return 0
    except VerificationError as exc:
        print(f"ccc-storage pack verify: {exc}")
        return 2


def _cmd_manifest_show(ns: argparse.Namespace) -> int:
    manifest = load_manifest(ns.manifest)
    print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None, *, prog: str = "ccc-storage pack") -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Build and inspect CCC immutable SquashFS packs.",
    )
    parser.add_argument("--version", action="version", version=f"{prog} {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    build = sub.add_parser("build", help="build a SquashFS pack from a source directory")
    build.add_argument("src")
    build.add_argument("out")
    build.add_argument("--manifest", help="write a child manifest pointing at the built pack")
    build.add_argument("--child-id")
    build.add_argument("--name")
    build.add_argument("--type", default="dataset")
    build.add_argument("--generation", type=int, default=1)
    build.add_argument("--revision", default="g1")
    build.add_argument("--comp", default="zstd")
    build.add_argument("--block", default="1M")
    build.add_argument("--exclude-boundary", action="append", default=[])
    build.set_defaults(func=_cmd_build)

    verify = sub.add_parser("verify", help="verify a pack checksum/size")
    verify.add_argument("pack")
    verify.add_argument("--sha256")
    verify.add_argument("--size", type=int)
    verify.add_argument("--file-count", type=int)
    verify.set_defaults(func=_cmd_verify)

    manifest = sub.add_parser("manifest", help="manifest operations")
    manifest_sub = manifest.add_subparsers(dest="manifest_cmd")
    show = manifest_sub.add_parser("show", help="show a TOML manifest as JSON")
    show.add_argument("manifest")
    show.set_defaults(func=_cmd_manifest_show)

    ns = parser.parse_args(argv)
    if hasattr(ns, "func"):
        return ns.func(ns)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
