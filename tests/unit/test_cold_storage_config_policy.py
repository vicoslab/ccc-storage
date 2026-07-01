from __future__ import annotations

from ccc_storage_cold.config import ColdStorageConfig
from ccc_storage_cold.policy import archive_decision, needs_recall
from ccc_storage_core.checksum import sha256_file
from ccc_storage_core.manifest import ChildManifest, PackInfo, PackStack, S3Info


def test_cold_config_uses_s3_env_as_backend_configuration():
    cfg = ColdStorageConfig.from_env(
        {
            "CCC_S3_BUCKET": "bucket",
            "CCC_S3_ENDPOINT": "https://s3.example.invalid",
            "CCC_COLD_STORAGE_PREFIX": "ccc/custom",
            "CCC_COLD_STORAGE_MIRROR_AFTER_COMMIT": "yes",
            "CCC_COLD_STORAGE_IDLE_SECONDS": "42",
        }
    )

    assert cfg.configured is True
    assert cfg.backend == "s3"
    assert cfg.archive_enabled is True
    assert cfg.mirror_after_commit is True
    assert cfg.idle_seconds == 42
    assert cfg.child_prefix("dataset:foo/bar", 7).startswith("ccc/custom/children/")
    assert cfg.child_prefix("dataset:foo/bar", 7).endswith("/g0007")


def test_cold_config_archive_can_be_explicitly_disabled_when_backend_configured():
    cfg = ColdStorageConfig.from_env(
        {
            "CCC_S3_BUCKET": "bucket",
            "CCC_S3_ENDPOINT": "https://s3.example.invalid",
            "CCC_COLD_STORAGE_ARCHIVE_ENABLED": "0",
        }
    )

    assert cfg.configured is True
    assert cfg.enabled is True
    assert cfg.archive_enabled is False


def test_cold_policy_needs_recall_for_cold_or_missing_hot_pack(tmp_path):
    pack = tmp_path / "pack.sqfs"
    pack.write_bytes(b"pack")
    hot = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pack_stack=PackStack(
            lowers=(PackInfo(path=str(pack), sha256=sha256_file(pack), size=pack.stat().st_size),)
        ),
        s3=S3Info(pack_state="hot"),
    )
    cold = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pack_stack=hot.pack_stack,
        s3=S3Info(pack_state="cold", uri="ccc/foo"),
    )
    missing = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pack_stack=PackStack(
            lowers=(PackInfo(path=str(tmp_path / "missing.sqfs"), sha256="a" * 64, size=1),)
        ),
        s3=S3Info(pack_state="hot"),
    )

    assert needs_recall(hot) is False
    assert needs_recall(cold) is True
    assert needs_recall(missing) is True


def test_cold_policy_requires_existing_access_metadata_before_archive(tmp_path):
    pack = tmp_path / "pack.sqfs"
    pack.write_bytes(b"pack")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pack_stack=PackStack(
            lowers=(PackInfo(path=str(pack), sha256=sha256_file(pack), size=pack.stat().st_size),)
        ),
        s3=S3Info(),
    )

    decision = archive_decision(manifest, dirty=False, mounted=False, idle_seconds=0, now=100)

    assert decision.eligible is False
    assert decision.reason == "no-access-metadata"


def test_cold_policy_marks_old_clean_hot_child_eligible(tmp_path):
    pack = tmp_path / "pack.sqfs"
    pack.write_bytes(b"pack")
    manifest = ChildManifest(
        id="dataset:foo",
        name="foo",
        type="dataset",
        generation=1,
        pack_stack=PackStack(
            lowers=(PackInfo(path=str(pack), sha256=sha256_file(pack), size=pack.stat().st_size),)
        ),
        s3=S3Info(last_accessed_at="2000-01-01T00:00:00Z"),
    )

    decision = archive_decision(manifest, dirty=False, mounted=False, idle_seconds=1)

    assert decision.eligible is True
    assert decision.reason == "eligible"
