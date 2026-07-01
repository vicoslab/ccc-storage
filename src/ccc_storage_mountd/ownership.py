"""Configurable UID/GID ownership helpers for mountd-created shared state."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Ownership:
    """Optional owner applied to storage data created by the mount daemon.

    ``None`` means "leave that id unchanged".  When both ids are ``None`` the
    policy is disabled and all methods are no-ops.
    """

    uid: int | None = None
    gid: int | None = None

    def __post_init__(self) -> None:
        for label, value in (("uid", self.uid), ("gid", self.gid)):
            if value is not None and value < 0:
                raise ValueError(f"{label} must be a non-negative integer")

    @property
    def enabled(self) -> bool:
        return self.uid is not None or self.gid is not None

    @property
    def attr_uid(self) -> int:
        """UID to expose in FUSE attrs when this policy owns a virtual entry."""

        return self.uid if self.uid is not None else os.getuid()

    @property
    def attr_gid(self) -> int:
        """GID to expose in FUSE attrs when this policy owns a virtual entry."""

        return self.gid if self.gid is not None else os.getgid()

    def apply(
        self,
        path: str | Path,
        *,
        recursive: bool = False,
        follow_symlinks: bool = True,
    ) -> None:
        """Apply this owner to *path* if configured.

        Recursive application never follows symlinked directories; symlinks are
        chowned as links.  Missing paths are ignored so callers can use this in
        cleanup/race-prone publish paths without masking other chown failures.
        """

        if not self.enabled:
            return
        target = Path(path)
        if not target.exists() and not target.is_symlink():
            return
        if recursive and target.is_dir() and not target.is_symlink():
            self._apply_tree(target)
            return
        self._chown_one(target, follow_symlinks=follow_symlinks)

    def apply_tree(self, path: str | Path) -> None:
        """Apply this owner to *path* and all descendants."""

        self.apply(path, recursive=True)

    def _chown_one(self, path: Path, *, follow_symlinks: bool = True) -> None:
        uid = self.uid if self.uid is not None else -1
        gid = self.gid if self.gid is not None else -1
        os.chown(path, uid, gid, follow_symlinks=follow_symlinks)

    def _apply_tree(self, root: Path) -> None:
        self._chown_one(root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(dirpath)
            for name in dirnames:
                child = base / name
                self._chown_one(child, follow_symlinks=not child.is_symlink())
            for name in filenames:
                child = base / name
                self._chown_one(child, follow_symlinks=not child.is_symlink())
