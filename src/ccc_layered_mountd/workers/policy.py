"""Deterministic auto-commit policy engine (D-12).

The policy is a pure function of cheap dirty-overlay accounting plus two
clock-derived ages, so tests evaluate the full decision table without FUSE or
sleeps. Defaults encode the D-12 thresholds:

* large dirty (``>=1 GiB``) commits once a 10-minute quiet period has elapsed,
* a changed-file count ``>=100k`` forces a commit regardless of quiet period,
* otherwise small dirty overlays commit on a weekly cadence (once quiet).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3

# Decision constants returned by :func:`evaluate`.
TRIGGER = "trigger"  # policy wants an auto-commit now
MANUAL = "manual"  # child is manual-only; never auto-commits
NOOP = "noop"  # nothing to do (clean, or thresholds not met)


@dataclass(frozen=True)
class CommitPolicy:
    """Per-child commit thresholds. ``mode='manual'`` disables auto-commit."""

    mode: str = "auto"
    max_dirty_bytes: int = GIB
    max_file_count: int = 100_000
    min_quiet_seconds: float = 600.0
    max_age_seconds: float = 7 * 24 * 3600.0


@dataclass(frozen=True)
class PolicyInputs:
    """Cheap dirty-overlay snapshot fed to :func:`evaluate`."""

    dirty: bool
    bytes: int
    file_count: int
    age_seconds: float  # now - oldest dirty write
    quiet_seconds: float  # now - newest dirty write


def evaluate(policy: CommitPolicy, inputs: PolicyInputs) -> str:
    """Return :data:`TRIGGER`, :data:`MANUAL`, or :data:`NOOP` for the inputs."""
    if not inputs.dirty:
        return NOOP
    if policy.mode == "manual":
        return MANUAL
    # A huge changed-file count forces a commit irrespective of the quiet gate.
    if inputs.file_count >= policy.max_file_count:
        return TRIGGER
    quiet_ok = inputs.quiet_seconds >= policy.min_quiet_seconds
    if inputs.bytes >= policy.max_dirty_bytes and quiet_ok:
        return TRIGGER
    if inputs.age_seconds >= policy.max_age_seconds and quiet_ok:
        return TRIGGER
    return NOOP


def overlay_inputs(active_upper: str | Path, *, now: float) -> PolicyInputs:
    """Scan a dirty upper into :class:`PolicyInputs` using an injected clock.

    ``now`` is supplied by the caller (the worker's clock) so the resulting ages
    are deterministic in tests. The scan is cheap relative to a commit and only
    runs on a worker tick, never on the hot read path.
    """
    upper = Path(active_upper)
    count = 0
    total = 0
    oldest: float | None = None
    newest: float | None = None
    if upper.exists():
        for entry in upper.rglob("*"):
            if not (entry.is_file() or entry.is_symlink()):
                continue
            count += 1
            try:
                st = entry.lstat()
            except OSError:
                continue
            total += st.st_size
            mtime = st.st_mtime
            oldest = mtime if oldest is None else min(oldest, mtime)
            newest = mtime if newest is None else max(newest, mtime)
    if count == 0:
        return PolicyInputs(dirty=False, bytes=0, file_count=0, age_seconds=0.0, quiet_seconds=0.0)
    return PolicyInputs(
        dirty=True,
        bytes=total,
        file_count=count,
        age_seconds=now - (oldest if oldest is not None else now),
        quiet_seconds=now - (newest if newest is not None else now),
    )
