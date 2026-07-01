"""Configurable log-structured pack levels: policy parser + pure planner.

This module is deliberately side-effect free. It computes *where* a pack belongs
and *which* packs a partial compaction should merge using only pack ``size`` and
``level`` metadata — never reading the filesystem. That keeps the planner unit
testable without SquashFS tools or FUSE.

Level model (see ``docs/design/log-structured-pack-levels.md``):

- Lower level number == larger capacity. ``L0`` is the big/stable base; higher
  level numbers are smaller/newer tiers. A fresh delta lands in the smallest
  (highest-numbered) level whose capacity can hold it.
- Invariant for a healthy stack: at most ``max_packs_per_level`` packs per level
  (default 1) and, ordered oldest→newest, levels strictly increase. A new commit
  that collides at a level, or a large pack that lands below newer tiers, breaks
  the invariant and the planner computes the minimal newest-suffix to merge.
- Partial compaction always merges a *contiguous newest run* so the merged
  pack's generation range stays contiguous and mount order (newest over oldest)
  is preserved. The target level is computed in one step from the merged byte
  total; the build is a single pack, not L5→L4→L3 rewrites.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# Documented default level shape (binary units). Tests use tiny custom levels.
DEFAULT_LEVELS_SPEC = "0:100G,1:10G,2:1G,3:100M,4:10M"
DEFAULT_MAX_ONLINE_COMPACTION = "10G"

_UNIT_FACTORS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}
_BYTES_RE = re.compile(r"^\s*(\d+)\s*([KMGTPB]?)(?:i?B)?\s*$", re.IGNORECASE)
_TRUE = {"1", "true", "yes", "on"}


class LevelPolicyError(ValueError):
    """Raised when a level spec / policy string is malformed."""


def parse_human_bytes(value: str | int) -> int:
    """Parse ``"100G"``/``"256K"``/``"1024"`` into a positive byte count."""
    if isinstance(value, int):
        if value <= 0:
            raise LevelPolicyError(f"byte size must be positive: {value!r}")
        return value
    text = str(value).strip()
    if not text:
        raise LevelPolicyError("empty byte size")
    match = _BYTES_RE.match(text)
    if not match:
        raise LevelPolicyError(f"malformed byte size: {value!r}")
    number = int(match.group(1))
    unit = match.group(2).upper()
    factor = _UNIT_FACTORS.get(unit)
    if factor is None:  # pragma: no cover - regex already constrains the unit
        raise LevelPolicyError(f"unknown byte unit in {value!r}")
    size = number * factor
    if size <= 0:
        raise LevelPolicyError(f"byte size must be positive: {value!r}")
    return size


@dataclass(frozen=True)
class PackLevel:
    level: int
    max_bytes: int
    name: str = ""


def parse_levels(spec: str) -> tuple[PackLevel, ...]:
    """Parse ``"0:100G,1:10G,..."`` into ``PackLevel`` tuples sorted by level."""
    if not spec or not spec.strip():
        raise LevelPolicyError("empty level spec")
    levels: dict[int, PackLevel] = {}
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        if ":" not in item:
            raise LevelPolicyError(f"malformed level entry (need 'level:size'): {item!r}")
        level_str, _, size_str = item.partition(":")
        level_str = level_str.strip()
        if not level_str.lstrip("-").isdigit():
            raise LevelPolicyError(f"malformed level number: {level_str!r}")
        level = int(level_str)
        if level < 0:
            raise LevelPolicyError(f"level number must be >= 0: {level}")
        if level in levels:
            raise LevelPolicyError(f"duplicate level: {level}")
        levels[level] = PackLevel(level=level, max_bytes=parse_human_bytes(size_str))
    if not levels:
        raise LevelPolicyError(f"no levels parsed from {spec!r}")
    return tuple(levels[key] for key in sorted(levels))


@dataclass(frozen=True)
class LevelPolicy:
    levels: tuple[PackLevel, ...]
    max_packs_per_level: int = 1
    allow_base_compaction: bool = False
    max_online_compaction_bytes: int = 10 * 1024**3
    trigger_after_commit: bool = True
    trigger_interval_seconds: float = 0.0

    @classmethod
    def default(cls) -> LevelPolicy:
        return cls(
            levels=parse_levels(DEFAULT_LEVELS_SPEC),
            max_online_compaction_bytes=parse_human_bytes(DEFAULT_MAX_ONLINE_COMPACTION),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LevelPolicy:
        """Build a policy from a ``CCC_*`` env-style mapping, falling back to
        documented defaults for any knob that is unset."""
        spec = env.get("CCC_PACK_LEVELS", "").strip() or DEFAULT_LEVELS_SPEC
        levels = parse_levels(spec)
        online_raw = env.get("CCC_MAX_ONLINE_COMPACTION_BYTES", "").strip()
        online = (
            parse_human_bytes(online_raw)
            if online_raw
            else parse_human_bytes(DEFAULT_MAX_ONLINE_COMPACTION)
        )
        max_packs_raw = env.get("CCC_MAX_PACKS_PER_LEVEL", "").strip()
        max_packs = int(max_packs_raw) if max_packs_raw.isdigit() and int(max_packs_raw) else 1
        interval_raw = env.get("CCC_COMPACT_INTERVAL_SECONDS", "").strip()
        try:
            interval = float(interval_raw) if interval_raw else 0.0
        except ValueError:
            interval = 0.0
        return cls(
            levels=levels,
            max_packs_per_level=max_packs,
            allow_base_compaction=_env_flag(env.get("CCC_ALLOW_BASE_COMPACTION"), False),
            max_online_compaction_bytes=online,
            trigger_after_commit=_env_flag(env.get("CCC_COMPACT_AFTER_COMMIT"), True),
            trigger_interval_seconds=interval,
        )

    def level_cap(self, level: int) -> int:
        for lvl in self.levels:
            if lvl.level == level:
                return lvl.max_bytes
        raise LevelPolicyError(f"unknown level: {level}")

    @property
    def base_level(self) -> int:
        return self.levels[0].level

    @property
    def top_level(self) -> int:
        return self.levels[-1].level


@dataclass(frozen=True)
class CompactionCandidate:
    packs: tuple[object, ...]
    target_level: int
    total_bytes: int
    blocked_reason: str = ""
    reason: str = ""


def _env_flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in _TRUE


def choose_initial_level(policy: LevelPolicy, size: int) -> int:
    """Smallest (highest-numbered) level whose capacity can hold *size*.

    A pack larger than every level capacity goes to the base level (``L0``).
    """
    chosen = policy.base_level
    for lvl in policy.levels:  # ascending by level number (largest cap first)
        if size <= lvl.max_bytes:
            chosen = lvl.level
    return chosen


def target_level_for(policy: LevelPolicy, total: int) -> int:
    """Smallest-capacity level that can hold *total*; base level if oversized."""
    return choose_initial_level(policy, total)


def _is_valid_stack(packs, policy: LevelPolicy) -> bool:
    """True if the stack (oldest→newest) already obeys the level invariant."""
    counts: dict[int, int] = {}
    prev_level: int | None = None
    for pack in packs:
        counts[pack.level] = counts.get(pack.level, 0) + 1
        if counts[pack.level] > policy.max_packs_per_level:
            return False
        if prev_level is not None and pack.level <= prev_level:
            # Newer packs must sit at a strictly higher level than older ones.
            return False
        prev_level = pack.level
    return True


def plan_level_compaction(
    packs,
    policy: LevelPolicy,
    *,
    allow_base: bool = False,
) -> CompactionCandidate | None:
    """Compute the minimal newest-suffix compaction needed to heal *packs*.

    ``packs`` is the full pack stack ordered oldest→newest (base first, as stored
    in ``PackStack.lowers``). Returns ``None`` when the stack already fits the
    level invariant, otherwise a :class:`CompactionCandidate` describing which
    packs to merge and the single target level. A candidate whose
    ``blocked_reason`` is non-empty means the merge is *needed* but disallowed by
    policy (base rewrite / online byte budget) and the caller must leave the
    stack untouched and surface the reason.
    """
    packs = tuple(packs)
    if len(packs) <= 1:
        return None
    if _is_valid_stack(packs, policy):
        return None

    allow_base = allow_base or policy.allow_base_compaction

    n = len(packs)
    for m in range(1, n + 1):
        group = packs[n - m :]
        prefix = packs[: n - m]
        if not _is_valid_stack(prefix, policy):
            continue
        total = sum(p.size for p in group)
        target = target_level_for(policy, total)
        prefix_max_level = max((p.level for p in prefix), default=-1)
        # The merged pack must land strictly above every untouched older pack so
        # the healed stack stays one-pack-per-level and generation-monotone.
        if target > prefix_max_level:
            reason = f"levels-overflow->L{target}"
            blocked = ""
            if target <= policy.base_level and not allow_base:
                blocked = (
                    f"base compaction into L{target} requires --allow-base "
                    "(maintenance window)"
                )
            elif total > policy.max_online_compaction_bytes and not allow_base:
                blocked = (
                    f"merge of {total} bytes exceeds online compaction budget "
                    f"{policy.max_online_compaction_bytes}; requires --allow-base"
                )
            return CompactionCandidate(
                packs=tuple(group),
                target_level=target,
                total_bytes=total,
                blocked_reason=blocked,
                reason=reason,
            )
    return None
