from __future__ import annotations

import pytest

from ccc_storage_core.manifest import (
    WRITE_POLICY_LOCAL_SSD_ASYNC,
    WRITE_POLICY_SHARED_NFS,
    ChildManifest,
    ManifestError,
    dump_atomic,
    load_manifest,
    normalize_write_policy,
)


def test_manifest_write_policy_defaults_to_shared_nfs_for_legacy_manifests(tmp_path):
    path = tmp_path / "legacy.toml"
    path.write_text(
        '\n'.join(
            [
                'schema_version = 1',
                'id = "observe:env"',
                'name = "env"',
                'type = "observed-child"',
                'generation = 0',
                '',
                '[overlay]',
                'mode = "shared-overlay"',
                'active_upper = "/nfs/overlays/env/active"',
                'overlay_generation = 0',
                '',
            ]
        )
    )

    loaded = load_manifest(path)

    assert loaded.write_policy == WRITE_POLICY_SHARED_NFS


def test_manifest_write_policy_roundtrips_local_ssd_async(tmp_path):
    path = tmp_path / "child.toml"
    manifest = ChildManifest(
        id="observe:env",
        name="env",
        type="observed-child",
        generation=0,
        write_policy=WRITE_POLICY_LOCAL_SSD_ASYNC,
    )

    dump_atomic(path, manifest)
    loaded = load_manifest(path)

    assert loaded.write_policy == WRITE_POLICY_LOCAL_SSD_ASYNC
    assert 'write_policy = "local-ssd-async"' in path.read_text()


def test_manifest_rejects_invalid_write_policy():
    with pytest.raises(ManifestError):
        normalize_write_policy("fast-magic")

    with pytest.raises(ManifestError):
        ChildManifest(
            id="observe:env",
            name="env",
            type="observed-child",
            generation=0,
            write_policy="fast-magic",
        ).to_dict()
