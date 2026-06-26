from __future__ import annotations

from ccc_layered_mountd.daemon import MountdService


def test_doctor_reports_active_submount_count(fake_nfs, tmp_path):
    service = MountdService(nfs_root=fake_nfs.ccc_layered, run_dir=tmp_path / "run")
    doc = service.handle_doctor()
    assert doc["active_submount_count"] == 0
