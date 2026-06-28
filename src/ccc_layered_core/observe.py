"""Marker-driven observation-root discovery.

Any directory containing ``CCC_LAYERED_OBSERVE`` is an observation root. Every
immediate subdirectory under an observation root is a child boundary. If roots
are nested, the deepest matching observation root owns paths below it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

OBSERVE_MARKER_NAME = "CCC_LAYERED_OBSERVE"


@dataclass(frozen=True)
class ObservationRoot:
    path: Path
    relative_path: str


@dataclass(frozen=True)
class ObservedChild:
    observation_root: ObservationRoot
    boundary_path: str
    child_name: str
    inner_path: str


def _normalize(rel_path: str | Path) -> str | None:
    raw = str(rel_path)
    if "\x00" in raw or raw.startswith("/"):
        return None
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "" if raw in ("", ".") else None
    value = str(PurePosixPath(raw))
    return "" if value == "." else value


def _join(*parts: str) -> str:
    clean = [part.strip("/") for part in parts if part.strip("/")]
    return "/".join(clean)


def _is_prefix(prefix: str, path: str) -> bool:
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


def discover_observation_roots(src: str | Path) -> tuple[ObservationRoot, ...]:
    """Return directories under *src* that contain the visible observe marker."""
    root = Path(src)
    if not root.is_dir():
        return ()
    roots: list[ObservationRoot] = []
    marker_at_root = root / OBSERVE_MARKER_NAME
    if marker_at_root.is_file():
        roots.append(ObservationRoot(path=root, relative_path=""))
    for marker in sorted(root.rglob(OBSERVE_MARKER_NAME)):
        if marker == marker_at_root or not marker.is_file():
            continue
        parent = marker.parent
        roots.append(
            ObservationRoot(
                path=parent,
                relative_path=parent.relative_to(root).as_posix(),
            )
        )
    return tuple(
        sorted(roots, key=lambda item: (item.relative_path.count("/"), item.relative_path))
    )


def immediate_child_boundaries(src: str | Path) -> tuple[str, ...]:
    """Return existing immediate child-boundary dirs for every observation root."""
    boundaries: list[str] = []
    for observed in discover_observation_roots(src):
        for entry in sorted(observed.path.iterdir(), key=lambda item: item.name):
            if entry.is_dir():
                boundaries.append(_join(observed.relative_path, entry.name))
    return tuple(boundaries)


def resolve_observed_child(
    src: str | Path,
    rel_path: str | Path,
    *,
    allow_missing: bool = False,
) -> ObservedChild | None:
    """Resolve *rel_path* to the child boundary selected by the nearest marker.

    With ``allow_missing=False`` the boundary directory must already exist. This
    is the path used for access. ``mkdir``-style callers can set
    ``allow_missing=True`` to resolve a new immediate child under an existing
    observation root.
    """
    root = Path(src)
    clean = _normalize(rel_path)
    if clean is None or not clean:
        return None

    best: ObservedChild | None = None
    best_depth = -1
    for observed in discover_observation_roots(root):
        obs_rel = observed.relative_path
        if not _is_prefix(obs_rel, clean):
            continue
        suffix = clean[len(obs_rel) :].lstrip("/") if obs_rel else clean
        if not suffix:
            continue
        child_name = suffix.split("/", 1)[0]
        boundary_path = _join(obs_rel, child_name)
        boundary_dir = root / boundary_path
        if not allow_missing and not boundary_dir.is_dir():
            continue
        if allow_missing and not observed.path.is_dir():
            continue
        depth = 0 if not obs_rel else len(obs_rel.split("/"))
        if depth > best_depth:
            inner_path = suffix[len(child_name) :].lstrip("/")
            best = ObservedChild(
                observation_root=observed,
                boundary_path=boundary_path,
                child_name=child_name,
                inner_path=inner_path,
            )
            best_depth = depth
    return best
