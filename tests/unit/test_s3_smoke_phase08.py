from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
S3_SMOKE = ROOT / "dev" / "validation" / "s3" / "s3-smoke.sh"


def test_s3_smoke_script_exists_and_is_executable():
    assert S3_SMOKE.exists()
    assert os.access(S3_SMOKE, os.X_OK)


def test_s3_smoke_uses_ceph_defaults_and_s3v4_auto_style():
    text = S3_SMOKE.read_text()
    object_store = (ROOT / "src" / "ccc_layered_hpc" / "object_store.py").read_text()
    assert "https://ceph-7.fri.uni-lj.si" in text
    assert "CCC_S3_ADDRESSING_STYLE:-auto" in text
    assert "signature_version" in object_store
    assert "s3v4" in object_store
    assert "request_checksum_calculation" in object_store
    assert "when_required" in object_store


def test_s3_smoke_sources_credentials_without_printing_or_xtrace():
    text = S3_SMOKE.read_text()
    assert ". \"$credentials_sh\"" in text
    assert "set -x" not in text
    assert "cat \"$credentials_sh\"" not in text
    assert "AWS_SECRET_ACCESS_KEY" in text
    assert "echo $AWS_SECRET_ACCESS_KEY" not in text
    assert "printenv" not in text
    assert "env |" not in text


def test_s3_smoke_validates_all_mirror_recall_paths():
    text = S3_SMOKE.read_text()
    for phrase in (
        "Boto3ObjectStore",
        "mirror_committed_packs",
        "recall_cold_pack",
        "store.exists(pack_key)",
        "store.read_bytes(pack_key)",
        "corrupt_recall_rejected",
        "store.delete_prefix",
        "PYTHONPATH",
    ):
        assert phrase in text


def test_s3_smoke_supports_temp_or_existing_bucket_modes():
    text = S3_SMOKE.read_text()
    assert "CCC_S3_BUCKET" in text
    assert "create=True" in text
    assert "create=False" in text
    assert "delete_bucket" in text
