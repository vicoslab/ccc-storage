from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "validation" / "docker" / "observation-runtime-smoke.sh"


def test_observation_runtime_smoke_exists_and_validates_marker_lazy_invariants():
    assert SCRIPT.exists()
    text = SCRIPT.read_text()
    for phrase in (
        "CCC_LAYERED_OBSERVE",
        "OBSERVE_MARKER_NAME",
        "immediate_child_boundaries(src)",
        "build_pack(src, root_pack_dir / 'base.sqfs', exclude_observed=True)",
        "observed pack namespaces are not separated",
        "observed user1 payload leaked into root pack",
        "observed env-a payload leaked into user1 pack",
        "user2_pack = build_pack(src / 'user2'",
        "service.mounts.active_count() != 0",
        "ccc-layered', 'observe-access'",
        "expected only user2 mounted after first access",
        "expected only env-a mounted after access",
    ):
        assert phrase in text
