"""NFS-safe lockfile helper using atomic O_CREAT|O_EXCL creation."""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path


class LockHeld(RuntimeError):
    """Raised when a lockfile already exists and is not stealable."""


@dataclass(frozen=True)
class LockInfo:
    node: str
    pid: int
    op: str
    acquired_ts: float
    heartbeat_ts: float

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def from_file(cls, path: str | Path) -> LockInfo:
        data = json.loads(Path(path).read_text())
        return cls(
            node=str(data.get("node", "")),
            pid=int(data.get("pid", 0)),
            op=str(data.get("op", "")),
            acquired_ts=float(data.get("acquired_ts", 0.0)),
            heartbeat_ts=float(data.get("heartbeat_ts", data.get("acquired_ts", 0.0))),
        )


class NFSLock:
    """Small lockfile abstraction suitable for NFS-backed control state."""

    def __init__(self, path: str | Path, *, op: str = "lock", stale_after: float = 3600.0) -> None:
        self.path = Path(path)
        self.op = op
        self.stale_after = stale_after
        self.info: LockInfo | None = None
        self._held = False

    def acquire(self, *, steal_stale: bool = False) -> NFSLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        info = LockInfo(socket.gethostname(), os.getpid(), self.op, now, now)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if steal_stale and self.is_stale():
                self.path.unlink()
                return self.acquire(steal_stale=False)
            raise LockHeld(f"lock already held: {self.path}") from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(info.to_json())
            f.flush()
            os.fsync(f.fileno())
        self.info = info
        self._held = True
        return self

    def heartbeat(self) -> None:
        if not self._held or self.info is None:
            raise RuntimeError("cannot heartbeat an unheld lock")
        now = time.time()
        self.info = LockInfo(
            self.info.node,
            self.info.pid,
            self.info.op,
            self.info.acquired_ts,
            now,
        )
        self.path.write_text(self.info.to_json())

    def is_stale(self) -> bool:
        if not self.path.exists():
            return False
        try:
            heartbeat_ts = LockInfo.from_file(self.path).heartbeat_ts
        except Exception:
            heartbeat_ts = self.path.stat().st_mtime
        return (time.time() - heartbeat_ts) > self.stale_after

    def release(self) -> None:
        if self._held and self.path.exists():
            self.path.unlink()
        self._held = False

    def __enter__(self) -> NFSLock:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.release()
