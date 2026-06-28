"""Filesystem namespace helpers shared by CCC layered components."""

from __future__ import annotations

from urllib.parse import quote

_SAFE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"


def safe_namespace_name(value: str) -> str:
    """Return a collision-resistant filesystem-safe name for an arbitrary id.

    This percent-encodes path separators, colons, percent signs, and other unsafe
    characters instead of lossy replacement. For example, ``observe:a/b`` and
    ``observe:a_b`` map to distinct names, preserving independent pack/overlay
    namespaces for marker-observed children.
    """
    text = str(value)
    if not text:
        return "child"
    return quote(text, safe=_SAFE_CHARS) or "child"
