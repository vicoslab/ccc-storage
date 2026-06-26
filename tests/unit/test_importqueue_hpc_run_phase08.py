from __future__ import annotations

from ccc_layered_hpc.hpc_run import FakeHpcTransport, HpcRunRequest, run_hpc_job
from ccc_layered_hpc.importqueue import ImportQueue, Provenance
from ccc_layered_pack.bundle import MountGraph, MountGraphNode


def test_import_queue_applies_delta_to_review_branch_and_promotes_explicitly(tmp_path):
    delta = tmp_path / "delta.tar"
    delta.write_bytes(b"delta")
    queue = ImportQueue(tmp_path / "queue")
    provenance = Provenance(site="hpc-a", job_id="42", root_id="home:alice", base_generation=3)

    record = queue.enqueue_delta(delta, branch="review-1", provenance=provenance)

    assert record.branch == "review-1"
    assert record.promoted is False
    assert (tmp_path / "queue" / "branches" / "review-1" / "provenance.json").exists()
    promoted = queue.promote("review-1")
    assert promoted.promoted is True


def test_hpc_run_uses_fake_transport_and_lands_output_delta_on_review_branch(tmp_path):
    pack = tmp_path / "home.sqfs"
    pack.write_bytes(b"home")
    graph = MountGraph(
        root="home:alice", included=(MountGraphNode(child_id="home:alice", path="."),)
    )
    queue = ImportQueue(tmp_path / "queue")
    transport = FakeHpcTransport(job_id="job-7", output_delta=b"result-delta")
    request = HpcRunRequest(site="hpc-a", branch="review-hpc", submit_argv=("sbatch", "job.slurm"))

    result = run_hpc_job(
        request,
        graph=graph,
        packs=[pack],
        workdir=tmp_path / "work",
        transport=transport,
        import_queue=queue,
    )

    assert result.job_id == "job-7"
    assert result.branch == "review-hpc"
    assert transport.submitted == [("sbatch", "job.slurm")]
    assert (
        tmp_path / "queue" / "branches" / "review-hpc" / "delta.tar"
    ).read_bytes() == b"result-delta"
