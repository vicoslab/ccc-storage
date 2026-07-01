"""Mockable external-HPC run orchestration foundation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ccc_storage_hpc.importqueue import ImportQueue, Provenance
from ccc_storage_pack.bundle import BundleEntry, MountGraph, build_packset_bundle


@dataclass(frozen=True)
class HpcRunRequest:
    site: str
    branch: str
    submit_argv: tuple[str, ...]


@dataclass(frozen=True)
class HpcRunResult:
    site: str
    job_id: str
    branch: str
    bundle_path: str


class FakeHpcTransport:
    """A deterministic no-network SSH/SLURM stand-in for unit tests."""

    def __init__(self, *, job_id: str, output_delta: bytes) -> None:
        self.job_id = job_id
        self.output_delta = output_delta
        self.submitted: list[tuple[str, ...]] = []

    def submit(self, bundle: Path, argv: tuple[str, ...]) -> str:
        _ = bundle
        self.submitted.append(argv)
        return self.job_id

    def collect_delta(self, job_id: str, dest: Path) -> None:
        if job_id != self.job_id:
            raise RuntimeError(f"unknown fake job: {job_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.output_delta)


def run_hpc_job(
    request: HpcRunRequest,
    *,
    graph: MountGraph,
    packs: list[Path],
    workdir: str | Path,
    transport: FakeHpcTransport,
    import_queue: ImportQueue,
) -> HpcRunResult:
    """Build a packset, fake-submit it, collect a delta, enqueue review branch."""
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    entries = [BundleEntry(str(path), f"packs/{path.name}") for path in packs]
    bundle = build_packset_bundle(work / "packset.tar", entries, graph)
    job_id = transport.submit(bundle, request.submit_argv)
    delta = work / "output-delta.tar"
    transport.collect_delta(job_id, delta)
    import_queue.enqueue_delta(
        delta,
        branch=request.branch,
        provenance=Provenance(
            site=request.site,
            job_id=job_id,
            root_id=graph.root,
            base_generation=0,
        ),
    )
    return HpcRunResult(
        site=request.site,
        job_id=job_id,
        branch=request.branch,
        bundle_path=str(bundle),
    )
