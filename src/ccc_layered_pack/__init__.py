"""ccc_layered_pack — SquashFS pack build/verify/read library and `ccc-pack` CLI.

Data-plane only: turns directory trees into immutable SquashFS packs and reads
them back (squashfuse unprivileged by default, kernel squashfs when privileged).
No daemon, no network.

Phase-00: package skeleton + CLI stub. Builder/reader/verify arrive in phase-01.
"""

__version__ = "0.0.0"
