"""Capability probe: struct shape and **internal consistency**.

We deliberately do NOT assert any particular host capability is True/False —
the probe reports the truth for whatever host runs it. We only assert the
relationships that must hold for the result to be self-consistent (a probe that
needs ``/dev/fuse`` cannot succeed while ``/dev/fuse`` is reported absent), per
the phase-00 acceptance criteria.
"""

from __future__ import annotations

import dataclasses

from tests.fakes import capability
from tests.fakes.capability import CAPS, Caps

_FIELDS = [f.name for f in dataclasses.fields(Caps)]


def test_caps_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(Caps)
    assert Caps.__dataclass_params__.frozen is True


def test_caps_all_fields_are_bool() -> None:
    d = dataclasses.asdict(CAPS)
    assert set(d) == set(_FIELDS)
    assert all(isinstance(v, bool) for v in d.values())


def test_probe_is_callable_and_returns_caps() -> None:
    result = capability.probe()
    assert isinstance(result, Caps)


def test_unpriv_fuse_implies_dev_fuse() -> None:
    # _probe_unpriv_fuse short-circuits to False unless /dev/fuse is usable and
    # a fusermount binary exists.
    if CAPS.unpriv_fuse:
        assert CAPS.dev_fuse
        assert CAPS.fusermount


def test_fuse_overlayfs_implies_dev_fuse() -> None:
    if CAPS.fuse_overlayfs:
        assert CAPS.dev_fuse
        assert CAPS.fusermount


def test_kernel_mounts_imply_mount_namespace() -> None:
    if CAPS.kernel_squashfs:
        assert CAPS.mountns
    if CAPS.kernel_overlay:
        assert CAPS.mountns


def test_mountns_implies_userns() -> None:
    # `unshare -rm` (user+mount ns) succeeding implies `unshare -r` (user ns)
    # also succeeds — the former is a superset.
    if CAPS.mountns:
        assert CAPS.userns


def test_caps_is_session_cached_singleton() -> None:
    # CAPS is computed once at import; re-importing yields the same object.
    from tests.fakes.capability import CAPS as caps_again

    assert caps_again is CAPS


def test_require_markers_exist() -> None:
    for name in (
        "require_dev_fuse",
        "require_unpriv_fuse",
        "require_kernel_mount",
        "require_fuse_overlay",
        "require_docker",
    ):
        assert hasattr(capability, name)
        assert getattr(capability, name) is not None


def test_probe_timeout_is_positive() -> None:
    assert capability.PROBE_TIMEOUT > 0
