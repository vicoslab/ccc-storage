"""ccc_layered_cli — the unprivileged user/container-facing CLI, `ccc-storage`.

The only lifecycle/control surface users and jobs touch. It does no mounting,
packing, or NFS mutation itself: it sends requests to the local mountd control
socket and renders responses, which keeps it safe to install in every
container. It may import only ``ccc_layered_core`` (enforced by an import-lint
test), never a sibling's internals.

Phase-00: package skeleton + CLI stub with an offline `doctor`. The real
`status/commit/...` over the socket arrive in phase-02+.
"""

__version__ = "0.0.0"
