"""Background maintenance workers for mountd (phase-05).

These run *off* the hot read path: an auto-commit policy engine
(:mod:`auto_commit`), a delta-pack compaction planner (:mod:`compaction`), and a
conservative retention/GC planner (:mod:`gc`). All workers expose deterministic
``tick()``/``poke()`` hooks so tests drive policy without wall-clock sleeps.
"""

from __future__ import annotations

__all__ = ["auto_commit", "compaction", "gc", "policy"]
