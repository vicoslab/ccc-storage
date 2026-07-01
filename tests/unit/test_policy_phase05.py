from __future__ import annotations

import os

import pytest

from ccc_storage_mountd.workers.policy import (
    MANUAL,
    NOOP,
    TRIGGER,
    CommitPolicy,
    PolicyInputs,
    evaluate,
    overlay_inputs,
)

GIB = 1024**3
WEEK = 7 * 24 * 3600


@pytest.mark.parametrize(
    ("inputs", "policy", "expected"),
    [
        # not dirty -> never commits
        (PolicyInputs(False, 0, 0, 0.0, 0.0), CommitPolicy(), NOOP),
        # manual mode -> never auto-triggers even when thresholds are tripped
        (
            PolicyInputs(True, 2 * GIB, 5, WEEK + 1, 10_000),
            CommitPolicy(mode="manual"),
            MANUAL,
        ),
        # large dirty but still being written (quiet not elapsed) -> wait
        (
            PolicyInputs(True, 2 * GIB, 5, 60.0, 30.0),
            CommitPolicy(),
            NOOP,
        ),
        # large dirty and quiet period elapsed -> commit
        (
            PolicyInputs(True, 2 * GIB, 5, 700.0, 700.0),
            CommitPolicy(),
            TRIGGER,
        ),
        # file-count over the cap forces a commit regardless of quiet period
        (
            PolicyInputs(True, 4096, 200_000, 5.0, 1.0),
            CommitPolicy(),
            TRIGGER,
        ),
        # small dirty but aged past the weekly window (and now quiet) -> commit
        (
            PolicyInputs(True, 4096, 3, WEEK + 1, WEEK + 1),
            CommitPolicy(),
            TRIGGER,
        ),
        # small + fresh + below all thresholds -> no-op
        (
            PolicyInputs(True, 4096, 3, 5.0, 1.0),
            CommitPolicy(),
            NOOP,
        ),
    ],
)
def test_policy_decision_table(inputs, policy, expected):
    assert evaluate(policy, inputs) == expected


def test_overlay_inputs_is_deterministic_with_injected_clock(tmp_path):
    upper = tmp_path / "active"
    upper.mkdir()
    (upper / "a.txt").write_text("hello")  # 5 bytes
    sub = upper / "d"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"x" * 10)  # 10 bytes

    # Deterministic mtimes: oldest write 1000, newest write 1500.
    os.utime(upper / "a.txt", (1000.0, 1000.0))
    os.utime(sub / "b.bin", (1500.0, 1500.0))

    inputs = overlay_inputs(upper, now=2000.0)

    assert inputs.dirty is True
    assert inputs.file_count == 2
    assert inputs.bytes == 15
    assert inputs.age_seconds == pytest.approx(1000.0)  # now - oldest
    assert inputs.quiet_seconds == pytest.approx(500.0)  # now - newest


def test_overlay_inputs_empty_upper_is_clean(tmp_path):
    upper = tmp_path / "active"
    upper.mkdir()
    inputs = overlay_inputs(upper, now=10.0)
    assert inputs == PolicyInputs(False, 0, 0, 0.0, 0.0)
