"""Import queue for external-HPC output deltas."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ccc_layered_core.checksum import sha256_file


@dataclass(frozen=True)
class Provenance:
    site: str
    job_id: str
    root_id: str
    base_generation: int


@dataclass(frozen=True)
class ImportRecord:
    branch: str
    delta_path: str
    sha256: str
    provenance: Provenance
    promoted: bool = False

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "delta_path": self.delta_path,
            "sha256": self.sha256,
            "provenance": asdict(self.provenance),
            "promoted": self.promoted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ImportRecord:
        return cls(
            branch=str(data["branch"]),
            delta_path=str(data["delta_path"]),
            sha256=str(data["sha256"]),
            provenance=Provenance(**data["provenance"]),
            promoted=bool(data.get("promoted", False)),
        )


class ImportQueue:
    """Queue incoming HPC deltas onto named review branches first."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _branch_dir(self, branch: str) -> Path:
        clean = branch.strip("/")
        if not clean or ".." in Path(clean).parts:
            raise ValueError(f"unsafe branch name: {branch!r}")
        return self.root / "branches" / clean

    def enqueue_delta(
        self, delta_bundle: str | Path, *, branch: str, provenance: Provenance
    ) -> ImportRecord:
        src = Path(delta_bundle)
        if not src.is_file():
            raise FileNotFoundError(src)
        branch_dir = self._branch_dir(branch)
        branch_dir.mkdir(parents=True, exist_ok=True)
        dest = branch_dir / "delta.tar"
        shutil.copyfile(src, dest)
        record = ImportRecord(
            branch=branch,
            delta_path=str(dest),
            sha256=sha256_file(dest),
            provenance=provenance,
        )
        (branch_dir / "provenance.json").write_text(
            json.dumps(record.to_dict(), indent=2, sort_keys=True)
        )
        return record

    def load(self, branch: str) -> ImportRecord:
        path = self._branch_dir(branch) / "provenance.json"
        return ImportRecord.from_dict(json.loads(path.read_text()))

    def promote(self, branch: str) -> ImportRecord:
        record = replace(self.load(branch), promoted=True)
        path = self._branch_dir(branch) / "provenance.json"
        path.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True))
        return record
