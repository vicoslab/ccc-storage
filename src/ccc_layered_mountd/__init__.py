"""ccc_layered_mountd — the per-node privileged daemon (runtime heart).

Owns host-visible mount authority: the shallow parent dispatcher FUSE, child
mount/unmount + refcount, overlay assembly, auto-commit/compaction workers, and
the control socket the CLI/HPC tools talk to. Node-local, never cluster-global:
authoritative truth is on NFS.

Phase-00: package skeleton + daemon stub (with a minimal capability doctor).
The minimal read-only daemon arrives in phase-02.
"""

__version__ = "0.0.0"
