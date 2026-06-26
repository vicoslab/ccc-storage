from __future__ import annotations

from ccc_layered_core.manifest import ChildManifest, PackInfo, PackStack
from ccc_layered_mountd.workers.gc import plan_gc


def _manifest(*, pinned=False):
    base = PackInfo(path="/p/base.sqfs", sha256="b" * 64, size=1000)
    return ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pinned=pinned,
        pack_stack=PackStack(active_revision="g1", lowers=(base,)),
    )


_RETIRED = (
    PackInfo(path="/p/old0.sqfs", sha256="0" * 64, size=10),
    PackInfo(path="/p/old1.sqfs", sha256="1" * 64, size=10),
)


def test_gc_evicts_safe_retired_packs_when_idle_and_clean():
    plan = plan_gc(
        _manifest(),
        _RETIRED,
        active_mount=False,
        dirty=False,
        pending_lock=False,
    )
    assert plan.evictable == _RETIRED
    assert plan.blocked == ()
    assert plan.reasons == ()


def test_gc_refuses_when_dirty_overlay():
    plan = plan_gc(_manifest(), _RETIRED, active_mount=False, dirty=True, pending_lock=False)
    assert plan.evictable == ()
    assert plan.blocked == _RETIRED
    assert "dirty-overlay" in plan.reasons


def test_gc_refuses_when_active_mount():
    plan = plan_gc(_manifest(), _RETIRED, active_mount=True, dirty=False, pending_lock=False)
    assert plan.evictable == ()
    assert "active-mount" in plan.reasons


def test_gc_refuses_when_pending_commit_lock():
    plan = plan_gc(_manifest(), _RETIRED, active_mount=False, dirty=False, pending_lock=True)
    assert plan.evictable == ()
    assert "pending-commit" in plan.reasons


def test_gc_refuses_when_pinned():
    plan = plan_gc(
        _manifest(pinned=True),
        _RETIRED,
        active_mount=False,
        dirty=False,
        pending_lock=False,
    )
    assert plan.evictable == ()
    assert "pinned" in plan.reasons


def test_admin_override_bypasses_mount_and_dirty_but_never_pinned():
    overridable = plan_gc(
        _manifest(),
        _RETIRED,
        active_mount=True,
        dirty=True,
        pending_lock=True,
        admin_override=True,
    )
    assert overridable.evictable == _RETIRED

    pinned = plan_gc(
        _manifest(pinned=True),
        _RETIRED,
        active_mount=False,
        dirty=False,
        pending_lock=False,
        admin_override=True,
    )
    assert pinned.evictable == ()
    assert "pinned" in pinned.reasons
