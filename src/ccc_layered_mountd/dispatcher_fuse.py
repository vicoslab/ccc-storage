"""Documented adapter placeholder for the shallow managed-parent FUSE dispatcher.

The transparent namespace at a managed parent (e.g. ``/managed/dataset``) is
ultimately presented by a pyfuse3 filesystem that implements **only** namespace
ops — ``lookup``/``getattr``/``readdir``/``mkdir``/``rmdir``/``rename`` — and
**never** ``read``/``write`` for child file content (RK-7). Once resolution
crosses into a child mount, the kernel / squashfuse serve the bytes.

This phase keeps the namespace decisions in :class:`managed_parent.ManagedParent`
(pure and unit-testable). pyfuse3 is imported lazily so the unit tier never
requires a FUSE-capable host; when it is unavailable, the adapter refuses with a
clear :class:`DispatcherUnavailable` instead of failing obscurely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ccc_layered_mountd.managed_parent import ManagedParent


class DispatcherUnavailable(RuntimeError):
    """Raised when the pyfuse3 dispatcher cannot be provided on this host."""


def _import_pyfuse3() -> Any:
    """Import pyfuse3 lazily. Separated out so tests can stub it."""
    import pyfuse3  # type: ignore[import-not-found]

    return pyfuse3


def available() -> bool:
    """True iff a real pyfuse3 dispatcher could be mounted on this host."""
    try:
        _import_pyfuse3()
    except Exception:
        return False
    return True


def mount_dispatcher(
    parent: ManagedParent,
    mountpoint: str,
    *,
    foreground: bool = True,
) -> None:
    """Mount the shallow namespace dispatcher for *parent* at *mountpoint*.

    Phase-04 ships the service-level namespace logic only; the pyfuse3 binding is
    not wired yet. Either way this refuses clearly: callers should use the
    :class:`~ccc_layered_mountd.managed_parent.ManagedParent` API (directly or via
    the mountd control socket) for namespace operations in this phase.
    """
    try:
        _import_pyfuse3()
    except Exception as exc:
        raise DispatcherUnavailable(
            "pyfuse3 is not importable on this host; the managed-parent FUSE "
            "dispatcher is unavailable. Use the ManagedParent service API or the "
            "mountd control socket for namespace operations."
        ) from exc
    raise DispatcherUnavailable(
        "the managed-parent FUSE dispatcher binding is not implemented in "
        "phase-04 (pyfuse3 is present). Namespace operations are available via "
        "the ManagedParent service API / mountd control socket."
    )
