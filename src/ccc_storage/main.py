"""Unified `ccc-storage` dispatcher.

The public CLI surface is one executable, `ccc-storage`.  Tool namespaces that
used to be separate executables live under explicit subcommands, while the
unprivileged mountd-control operations remain direct commands:

- `ccc-storage pack ...`
- `ccc-storage mountd ...`
- `ccc-storage hpc ...`
- `ccc-storage conda ...`
- `ccc-storage mamba ...`
- `ccc-storage benchmark ...`
- `ccc-storage status|commit|doctor|...`
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from ccc_storage_bench import perf as bench_cli
from ccc_storage_cli import __version__, conda_shim
from ccc_storage_cli.main import CONTROL_COMMANDS, main as control_main
from ccc_storage_hpc.client import main as hpc_main
from ccc_storage_mountd.daemon import main as mountd_main
from ccc_storage_pack.cli import main as pack_main

Dispatcher = Callable[[list[str]], int]

_TOOL_HELP = (
    ("pack", "build, verify, and inspect immutable SquashFS packs"),
    ("mountd", "run the per-node privileged mount/control daemon"),
    ("hpc", "stage external-HPC packsets and import/export deltas"),
    ("conda", "run the conservative managed-env conda shim"),
    ("mamba", "run the conservative managed-env mamba shim"),
    ("benchmark", "run deterministic small-file write/read benchmarks"),
)

_DISPATCH: dict[str, Dispatcher] = {
    "pack": lambda argv: pack_main(argv, prog="ccc-storage pack"),
    "mountd": lambda argv: mountd_main(argv, prog="ccc-storage mountd"),
    "hpc": lambda argv: hpc_main(argv, prog="ccc-storage hpc"),
    "conda": conda_shim.main_conda,
    "mamba": conda_shim.main_mamba,
    "benchmark": lambda argv: bench_cli.main(argv, prog="ccc-storage benchmark"),
    # Accept the misspelling from early design notes as a hidden compatibility
    # alias, but keep docs/help on the correctly-spelled command.
    "bechmark": lambda argv: bench_cli.main(argv, prog="ccc-storage benchmark"),
}


def _format_help() -> str:
    tool_width = max(len(name) for name, _ in _TOOL_HELP)
    control_width = max(len(name) for name in CONTROL_COMMANDS)
    control_lines = "\n".join(f"  {name:<{control_width}}  mountd control operation" for name in CONTROL_COMMANDS)
    tool_lines = "\n".join(f"  {name:<{tool_width}}  {desc}" for name, desc in _TOOL_HELP)
    choices = ",".join([name for name, _ in _TOOL_HELP] + list(CONTROL_COMMANDS))
    return f"""usage: ccc-storage [-h] [--version] {{{choices}}} ...

Unified CCC storage CLI.

options:
  -h, --help   show this help message and exit
  --version    show program's version number and exit

tool namespaces:
{tool_lines}

mountd control operations:
{control_lines}

examples:
  ccc-storage doctor
  ccc-storage pack build SRC OUT.sqfs
  ccc-storage mountd --nfs-root /storage/.ccc-storage --run-dir /run/ccc-storage
  ccc-storage conda install -n env numpy

Use `ccc-storage <command> --help` for command-specific options.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_format_help())
        return 0
    if args[0] == "--version":
        print(f"ccc-storage {__version__}")
        raise SystemExit(0)

    cmd, rest = args[0], args[1:]
    dispatcher = _DISPATCH.get(cmd)
    if dispatcher is not None:
        return dispatcher(rest)

    # All existing unprivileged mountd-control operations stay direct:
    # `ccc-storage status ...`, `ccc-storage commit ...`, etc.
    return control_main(args, prog="ccc-storage")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
