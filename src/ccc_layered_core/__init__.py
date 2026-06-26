"""ccc_layered_core — shared contracts for the layered storage system.

This package holds the contracts every other component agrees on: the manifest
TOML schema, NFS-safe lock protocol, control-socket protocol types, and
absolute-path -> owning-boundary resolution. It has **no** FUSE, privilege, or
network dependencies, which is what lets the other four packages stay
independent of each other.

Phase-00: package skeleton only. The real contracts arrive with phase-01.
"""

__version__ = "0.0.0"
