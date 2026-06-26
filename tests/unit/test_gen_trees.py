"""Synthetic-tree generators: determinism, dataset shape, conda-like env shape."""

from __future__ import annotations

import os

from tests.fakes import gen_trees


def _digest_tree(root) -> list[tuple[str, bytes]]:
    """Sorted (relative-path, bytes) for every regular file under *root*."""
    out: list[tuple[str, bytes]] = []
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            rel = os.path.relpath(p, root)
            out.append((rel, open(p, "rb").read()))
    return sorted(out)


def test_make_dataset_count_and_size(tmp_path) -> None:
    root = gen_trees.make_dataset(tmp_path / "ds", count=50, size=128, shard=10)
    bins = [p for p in root.rglob("*.bin")]
    assert len(bins) == 50
    assert all(p.stat().st_size == 128 for p in bins)
    # Sharded into ceil(50/10) = 5 shard dirs.
    shard_dirs = {p.name for p in root.iterdir() if p.is_dir()}
    assert len(shard_dirs) == 5


def test_make_dataset_is_byte_deterministic(tmp_path) -> None:
    a = gen_trees.make_dataset(tmp_path / "a", count=200, size=64)
    b = gen_trees.make_dataset(tmp_path / "b", count=200, size=64)
    assert _digest_tree(a) == _digest_tree(b)


def test_make_dataset_content_varies_by_index(tmp_path) -> None:
    root = gen_trees.make_dataset(tmp_path / "v", count=5, size=64, shard=100)
    contents = {p.read_bytes() for p in root.rglob("*.bin")}
    assert len(contents) == 5  # each index distinct


def test_make_dataset_zero_size(tmp_path) -> None:
    root = gen_trees.make_dataset(tmp_path / "z", count=3, size=0)
    bins = list(root.rglob("*.bin"))
    assert len(bins) == 3
    assert all(p.stat().st_size == 0 for p in bins)


def test_conda_like_env_shape(tmp_path) -> None:
    root = gen_trees.make_conda_like_env(tmp_path / "env")
    py = root / "bin" / "python3.11"
    alias = root / "bin" / "python3"
    script = root / "bin" / "ccc-tool"
    init = root / "lib" / "python3.11" / "site-packages" / "cccpkg" / "__init__.py"

    assert py.is_file()
    assert alias.is_symlink()
    assert os.readlink(alias) == "python3.11"
    assert script.is_file()
    # Executable shebang script.
    assert script.read_text().startswith("#!")
    assert os.access(script, os.X_OK)
    assert init.read_text() == "VALUE = 42\n"


def test_conda_like_env_is_deterministic(tmp_path) -> None:
    a = gen_trees.make_conda_like_env(tmp_path / "a")
    b = gen_trees.make_conda_like_env(tmp_path / "b")
    assert _digest_tree(a) == _digest_tree(b)


def test_conda_like_env_idempotent(tmp_path) -> None:
    dest = tmp_path / "env"
    gen_trees.make_conda_like_env(dest)
    # Second call over the same dir must not raise (symlink/hardlink exist).
    gen_trees.make_conda_like_env(dest)
    assert (dest / "bin" / "python3").is_symlink()


def test_corrupt_flips_byte(tmp_path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"\x00\x00\x00\x00")
    gen_trees.corrupt(p, offset=1)
    assert p.read_bytes() == b"\x00\xff\x00\x00"


def test_corrupt_set_specific_byte(tmp_path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"ABCD")
    gen_trees.corrupt(p, offset=0, byte=0x7A)
    assert p.read_bytes()[0] == 0x7A


def test_corrupt_extends_when_offset_past_end(tmp_path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"AB")
    gen_trees.corrupt(p, offset=5, byte=0x01)
    data = p.read_bytes()
    assert len(data) == 6
    assert data[5] == 0x01
