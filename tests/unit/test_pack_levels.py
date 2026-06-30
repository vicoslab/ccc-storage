from __future__ import annotations

import pytest

from ccc_layered_core.manifest import PackInfo
from ccc_layered_mountd.workers.levels import (
    LevelPolicy,
    LevelPolicyError,
    PackLevel,
    choose_initial_level,
    parse_human_bytes,
    parse_levels,
    plan_level_compaction,
)

GiB = 1024**3
MiB = 1024**2
KiB = 1024


# --- byte parsing ------------------------------------------------------------


def test_parse_human_bytes_units():
    assert parse_human_bytes("100G") == 100 * GiB
    assert parse_human_bytes("10M") == 10 * MiB
    assert parse_human_bytes("256K") == 256 * KiB
    assert parse_human_bytes("1024") == 1024


def test_parse_human_bytes_rejects_malformed():
    with pytest.raises(LevelPolicyError):
        parse_human_bytes("10X")
    with pytest.raises(LevelPolicyError):
        parse_human_bytes("")
    with pytest.raises(LevelPolicyError):
        parse_human_bytes("0M")


# --- level spec parsing ------------------------------------------------------


def test_parse_levels_sorted():
    levels = parse_levels("3:100M,0:100G,4:10M,1:10G,2:1G")
    assert [lvl.level for lvl in levels] == [0, 1, 2, 3, 4]
    assert levels[0].max_bytes == 100 * GiB
    assert levels[-1].max_bytes == 10 * MiB


def test_parse_levels_rejects_duplicate_levels():
    with pytest.raises(LevelPolicyError):
        parse_levels("0:100G,0:10G")


def test_parse_levels_rejects_nonpositive_capacity():
    with pytest.raises(LevelPolicyError):
        parse_levels("0:0")


def test_parse_levels_rejects_malformed_units():
    with pytest.raises(LevelPolicyError):
        parse_levels("0:100Q")
    with pytest.raises(LevelPolicyError):
        parse_levels("notalevel")


# --- initial level choice ----------------------------------------------------


def _default_policy() -> LevelPolicy:
    return LevelPolicy(levels=parse_levels("0:100G,1:10G,2:1G,3:100M,4:10M"))


def test_choose_initial_level_picks_smallest_holding_level():
    policy = _default_policy()
    # 6 MiB fits the smallest level (L4 = 10M).
    assert choose_initial_level(policy, 6 * MiB) == 4
    # 800 MiB only fits L2 (1G) or larger; smallest such is L2.
    assert choose_initial_level(policy, 800 * MiB) == 2
    # 50 MiB fits L3 (100M).
    assert choose_initial_level(policy, 50 * MiB) == 3


def test_choose_initial_level_oversized_goes_to_base():
    policy = _default_policy()
    # Bigger than every level cap -> base level 0.
    assert choose_initial_level(policy, 500 * GiB) == 0


# --- planner -----------------------------------------------------------------


def _pack(path, size, level, gen):
    return PackInfo(
        path=path,
        sha256="a" * 64,
        size=size,
        level=level,
        generation_min=gen,
        generation_max=gen,
        kind="delta" if level else "base",
    )


def test_plan_noop_when_levels_fit():
    policy = _default_policy()
    packs = (
        _pack("/base.sqfs", 50 * GiB, 0, 1),
        _pack("/l3.sqfs", 80 * MiB, 3, 5),
        _pack("/l4.sqfs", 6 * MiB, 4, 6),
    )
    assert plan_level_compaction(packs, policy) is None


def test_plan_compacts_top_levels_into_l3():
    policy = _default_policy()
    # L4 already at 8M, a fresh 6M delta also landed at L4 -> overflow.
    packs = (
        _pack("/base.sqfs", 50 * GiB, 0, 1),
        _pack("/l2.sqfs", 500 * MiB, 2, 3),
        _pack("/l3.sqfs", 80 * MiB, 3, 5),
        _pack("/l4.sqfs", 8 * MiB, 4, 6),
        _pack("/new.sqfs", 6 * MiB, 4, 7),
    )
    cand = plan_level_compaction(packs, policy)
    assert cand is not None
    assert cand.target_level == 3
    selected = {p.path for p in cand.packs}
    assert selected == {"/l3.sqfs", "/l4.sqfs", "/new.sqfs"}
    assert cand.total_bytes == 94 * MiB
    assert cand.blocked_reason == ""
    # Must not touch L2/L1/L0.
    assert "/l2.sqfs" not in selected
    assert "/base.sqfs" not in selected


def test_plan_compacts_into_l1_when_l2_overflows():
    policy = _default_policy()
    # New 800M delta lands at L2 where 700M already sits -> overflow; smaller
    # newer levels are swept in to keep the merged generation range contiguous.
    packs = (
        _pack("/base.sqfs", 50 * GiB, 0, 1),
        _pack("/l2.sqfs", 700 * MiB, 2, 3),
        _pack("/l4.sqfs", 4 * MiB, 4, 5),
        _pack("/new.sqfs", 800 * MiB, 2, 6),
    )
    cand = plan_level_compaction(packs, policy)
    assert cand is not None
    assert cand.target_level == 1
    selected = {p.path for p in cand.packs}
    assert selected == {"/l2.sqfs", "/l4.sqfs", "/new.sqfs"}
    assert "/base.sqfs" not in selected


def test_plan_blocks_base_compaction_when_disallowed():
    policy = LevelPolicy(
        levels=parse_levels("0:100M,1:10M"),
        allow_base_compaction=False,
    )
    # Everything would have to merge into L0 (base) -> blocked.
    packs = (
        _pack("/base.sqfs", 60 * MiB, 0, 1),
        _pack("/l1.sqfs", 8 * MiB, 1, 2),
        _pack("/new.sqfs", 8 * MiB, 1, 3),
    )
    cand = plan_level_compaction(packs, policy)
    assert cand is not None
    assert cand.target_level == 0
    assert cand.blocked_reason
    assert "base" in cand.blocked_reason.lower()


def test_plan_allows_base_compaction_when_enabled():
    policy = LevelPolicy(
        levels=parse_levels("0:100M,1:10M"),
        allow_base_compaction=True,
    )
    packs = (
        _pack("/base.sqfs", 60 * MiB, 0, 1),
        _pack("/l1.sqfs", 8 * MiB, 1, 2),
        _pack("/new.sqfs", 8 * MiB, 1, 3),
    )
    cand = plan_level_compaction(packs, policy)
    assert cand is not None
    assert cand.target_level == 0
    assert cand.blocked_reason == ""
    assert {p.path for p in cand.packs} == {"/base.sqfs", "/l1.sqfs", "/new.sqfs"}


def test_plan_blocks_when_over_online_budget():
    policy = LevelPolicy(
        levels=parse_levels("0:100G,1:10G,2:1G,3:100M,4:10M"),
        max_online_compaction_bytes=1 * GiB,
        allow_base_compaction=False,
    )
    packs = (
        _pack("/base.sqfs", 50 * GiB, 0, 1),
        _pack("/l2.sqfs", 700 * MiB, 2, 3),
        _pack("/new.sqfs", 800 * MiB, 2, 6),
    )
    cand = plan_level_compaction(packs, policy)
    assert cand is not None
    assert cand.blocked_reason
    assert "budget" in cand.blocked_reason.lower() or "online" in cand.blocked_reason.lower()


def test_plan_computes_single_final_target_not_intermediate():
    policy = _default_policy()
    # Cascade L4->L3->L2 should resolve to a single target, not three rewrites.
    packs = (
        _pack("/base.sqfs", 50 * GiB, 0, 1),
        _pack("/l2.sqfs", 900 * MiB, 2, 2),
        _pack("/l3.sqfs", 90 * MiB, 3, 3),
        _pack("/l4.sqfs", 9 * MiB, 4, 4),
        _pack("/new.sqfs", 9 * MiB, 4, 5),
    )
    cand = plan_level_compaction(packs, policy)
    assert cand is not None
    # 9+9 -> 18M (>L4) ; +90 -> 108M (>L3 100M) ; +900 -> ~1008M (>L2 1G? 1008<1024) fits L2.
    assert cand.target_level == 2
    assert {p.path for p in cand.packs} == {
        "/l2.sqfs",
        "/l3.sqfs",
        "/l4.sqfs",
        "/new.sqfs",
    }


def test_plan_does_not_leave_invalid_legacy_prefix():
    policy = _default_policy()
    packs = (
        _pack("/base.sqfs", 50 * GiB, 0, 1),
        # Legacy delta loaded without level metadata defaults to L0.
        _pack("/legacy-delta.sqfs", 10 * MiB, 0, 2),
        _pack("/new.sqfs", 6 * MiB, 4, 3),
    )

    cand = plan_level_compaction(packs, policy)

    assert cand is not None
    assert {pack.path for pack in cand.packs} == {"/legacy-delta.sqfs", "/new.sqfs"}
    assert cand.target_level == 3


# --- policy parsing from env-like config -------------------------------------


def test_level_policy_from_env_parses_all_knobs():
    policy = LevelPolicy.from_env(
        {
            "CCC_PACK_LEVELS": "0:100G,1:10G,2:1G,3:100M,4:10M",
            "CCC_MAX_ONLINE_COMPACTION_BYTES": "10G",
            "CCC_ALLOW_BASE_COMPACTION": "0",
            "CCC_COMPACT_AFTER_COMMIT": "1",
        }
    )
    assert [lvl.level for lvl in policy.levels] == [0, 1, 2, 3, 4]
    assert policy.max_online_compaction_bytes == 10 * GiB
    assert policy.allow_base_compaction is False
    assert policy.trigger_after_commit is True


def test_level_policy_default_is_usable():
    policy = LevelPolicy.default()
    assert policy.levels
    assert choose_initial_level(policy, 1) == policy.levels[-1].level


def test_pack_level_dataclass_fields():
    lvl = PackLevel(level=2, max_bytes=1 * GiB, name="medium")
    assert lvl.level == 2
    assert lvl.max_bytes == 1 * GiB
    assert lvl.name == "medium"
