from __future__ import annotations

from ccc_storage_cold.object_store import LocalObjectStore
from ccc_storage_hpc.hpc_s3_exchange import (
    fetch_hpc_packset_bundle,
    import_hpc_delta_from_s3,
    publish_hpc_import_delta,
    publish_hpc_packset_bundle,
)
from ccc_storage_hpc.importqueue import ImportQueue, Provenance
from ccc_storage_pack.bundle import (
    BundleEntry,
    MountGraph,
    MountGraphNode,
    build_packset_bundle,
    unpack_packset_bundle,
)


def test_hpc_packset_bundle_upload_download_and_unpack_roundtrip(tmp_path):
    pack = tmp_path / "dataset-delta.sqfs"
    pack.write_bytes(b"pack bytes for external hpc")
    graph = MountGraph(
        root="dataset:photos",
        included=(MountGraphNode(child_id="dataset:photos", path="."),),
        excluded=(
            MountGraphNode(
                child_id="env:private",
                path="conda/envs/private",
                reason="not selected",
            ),
        ),
    )
    bundle = build_packset_bundle(
        tmp_path / "packset.tar",
        [BundleEntry(str(pack), "packs/dataset-delta.sqfs")],
        graph,
    )
    store = LocalObjectStore(tmp_path / "objects")

    record = publish_hpc_packset_bundle(
        store,
        bundle,
        prefix="ccc/hpc/jobs/job-1/input",
        site="hpc-a",
        root_id="dataset:photos",
        generation=2,
    )

    assert record.bundle_key == "ccc/hpc/jobs/job-1/input/packset.tar"
    assert store.exists(record.bundle_key)
    assert store.exists("ccc/hpc/jobs/job-1/input/exchange-record.json")

    downloaded = fetch_hpc_packset_bundle(store, record, tmp_path / "downloaded-packset.tar")
    unpacked = unpack_packset_bundle(downloaded, tmp_path / "unpacked")

    assert unpacked.graph.root == "dataset:photos"
    assert unpacked.graph.excluded[0].child_id == "env:private"
    unpacked_pack = tmp_path / "unpacked" / "packs" / "dataset-delta.sqfs"
    assert unpacked_pack.read_bytes() == pack.read_bytes()


def test_hpc_output_delta_upload_and_import_queue_roundtrip(tmp_path):
    delta = tmp_path / "output-delta.tar"
    delta.write_bytes(b"external hpc output delta")
    store = LocalObjectStore(tmp_path / "objects")
    provenance = Provenance(
        site="hpc-a",
        job_id="slurm-42",
        root_id="dataset:photos",
        base_generation=2,
    )

    uploaded = publish_hpc_import_delta(
        store,
        delta,
        prefix="ccc/hpc/import/slurm-42",
        branch="hpc-review-slurm-42",
        provenance=provenance,
    )

    assert uploaded.delta_key == "ccc/hpc/import/slurm-42/output-delta.tar"
    assert store.exists(uploaded.delta_key)
    assert store.exists("ccc/hpc/import/slurm-42/import-record.json")

    queue = ImportQueue(tmp_path / "queue")
    record = import_hpc_delta_from_s3(store, "ccc/hpc/import/slurm-42", queue)

    assert record.branch == "hpc-review-slurm-42"
    assert record.provenance == provenance
    assert record.promoted is False
    queued_delta = tmp_path / "queue" / "branches" / "hpc-review-slurm-42" / "delta.tar"
    assert queued_delta.read_bytes() == delta.read_bytes()
