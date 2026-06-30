# Conda-like small-file benchmark

Command run locally from this repo:

```bash
PATH=/path/to/ccc-dev/bin:$PATH \
python dev/bench/conda_small_files.py \
  --files 3000 \
  --payload-bytes 256 \
  --json-out dev/docs/performance/conda-small-files-smoke.json
```

Result on the current CCC workspace node:

| Metric | Value |
|---|---:|
| Synthetic files | 3,000 |
| Raw payload bytes | 768,000 |
| SquashFS pack size | 36,864 bytes |
| Raw stat traversal | 0.847258 s |
| SquashFS build/commit pack step | 1.810742 s |
| Stat one pack object | 0.000823 s |
| Raw-to-pack compression ratio | 20.833x |
| NFS objects after commit | 1 pack for 3,000 files |

Interpretation:

- The benchmark intentionally models conda-style small files: Python modules,
  package data, `conda-meta` JSON, headers, and small executables.
- The important operational win is metadata collapse: after commit, shared
  storage sees one SquashFS object instead of thousands of files.
- This local smoke does not replace a real NFS benchmark, but it validates the
  expected order-of-magnitude behavior and gives a repeatable command for future
  node/NFS comparisons.
