from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLD_HPC_SMOKE = ROOT / "deploy" / "validation" / "s3" / "s3-cold-hpc-smoke.sh"


def test_s3_cold_hpc_smoke_script_exists_and_is_executable():
    assert COLD_HPC_SMOKE.exists()
    assert os.access(COLD_HPC_SMOKE, os.X_OK)


def test_s3_cold_hpc_smoke_uses_real_commit_and_cold_archive_paths():
    text = COLD_HPC_SMOKE.read_text()
    for phrase in (
        "MountdService",
        "handle_commit",
        "archive_committed_packs_to_cold_storage",
        "recall_cold_pack",
        "unsquashfs",
        "frames/image-001.txt",
    ):
        assert phrase in text


def test_s3_cold_hpc_smoke_validates_external_hpc_exchange_without_external_hpc():
    text = COLD_HPC_SMOKE.read_text()
    for phrase in (
        "build_packset_bundle",
        "publish_hpc_packset_bundle",
        "fetch_hpc_packset_bundle",
        "unpack_packset_bundle",
        "publish_hpc_import_delta",
        "import_hpc_delta_from_s3",
        "ImportQueue",
        "Provenance",
    ):
        assert phrase in text


def test_s3_cold_hpc_smoke_sources_credentials_without_printing_or_xtrace():
    text = COLD_HPC_SMOKE.read_text()
    assert ". \"$credentials_sh\"" in text
    assert "set -x" not in text
    assert "cat \"$credentials_sh\"" not in text
    assert "echo $AWS_SECRET_ACCESS_KEY" not in text
    assert "printenv" not in text
    assert "env |" not in text
