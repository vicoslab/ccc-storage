from __future__ import annotations

from ccc_storage_mountd.managed_parent import is_internal_name, visible_entries
from ccc_storage_pack.builder import BOUNDARY_MARKER_NAME


def test_visible_entries_hides_boundary_markers_but_keeps_names():
    entries = [BOUNDARY_MARKER_NAME, "env-a", "env-b", ".ccc-storage"]
    assert visible_entries(entries) == ["env-a", "env-b"]


def test_is_internal_name_hides_marker_not_boundary_name():
    assert is_internal_name(BOUNDARY_MARKER_NAME)
    assert not is_internal_name("env-a")
