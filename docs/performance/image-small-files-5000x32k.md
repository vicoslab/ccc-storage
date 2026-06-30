# Image-like small-file benchmark

Real Docker/FUSE benchmark run on `donbot` inside the `ccc-layered-mountd:phase14` image.

## Workload

- Files: 5,000
- Size per file: 32 KiB
- Total payload: 156.25 MiB
- Payloads: JPEG-like pseudo-random bytes, intentionally mostly incompressible.
- Result root: `/storage/user/ccc-layered-storage-image-bench/runs/donbot-20260630T100156Z-14437`

## Mounts

- `/bench/nfs`: `/bench/nfs 10.10.20.31:/nfs/LUVSS_home/vicos/domen.tabernik@fri.uni-lj.si/ccc-layered-storage-image-bench/runs/donbot-20260630T100156Z-14437/nfs nfs4 rw,relatime,vers=4.0,rsize=1048576,wsize=1048576,namlen=255,hard,proto=tcp,timeo=600,retrans=2,sec=sys,clientaddr=10.10.20.75,fsc,local_lock=none,addr=10.10.20.31`
- `/bench/ssd`: `/bench/ssd /dev/md0[/opt/storage/ssd/domen.tabernik@fri.uni-lj.si/ccc-layered-storage-image-bench/runs/donbot-20260630T100156Z-14437/ssd] ext4 rw,relatime,stripe=256`

## Results

| Operation | Seconds | Files/s | MiB/s | Notes |
|---|---:|---:|---:|---|
| Write direct SSD | 0.507990 | 9842.72 | 307.58 | includes sync=0.177361s |
| Write direct NFS | 7.938545 | 629.84 | 19.68 | includes sync=0.008883s |
| Read direct SSD | 0.516686 | 9677.06 | 302.41 |  |
| Read direct NFS | 3.401626 | 1469.89 | 45.93 |  |
| Build SquashFS from NFS | 2.235663 | 2236.47 | 69.89 | pack=163,885,056 bytes |
| Read SquashFS pack on NFS | 2.208082 | 2264.41 | 70.76 |  |
| Read overlay lower = SquashFS | 2.879384 | 1736.48 | 54.27 |  |
| Write dirty overlay upper on NFS | 12.360844 | 404.50 | 12.64 | includes sync=0.011243s |
| Read dirty overlay upper on NFS | 3.753004 | 1332.27 | 41.63 |  |

## Read comparison

| Read path | vs direct NFS | vs direct SSD |
|---|---:|---:|
| Direct NFS | 1.000x | 0.152x |
| SquashFS pack on NFS | 1.541x | 0.234x |
| Overlay lower = SquashFS | 1.181x | 0.179x |
| Dirty overlay upper on NFS | 0.906x | 0.138x |

## Takeaways

- Reading directly from a SquashFS pack stored on NFS was **1.54x faster than direct NFS small-file reads** for this workload, but still only **0.23x of local SSD**.
- Reading through the full writable overlay stack with SquashFS as lower was **1.18x faster than direct NFS** and **0.18x of local SSD**.
- Dirty overlay upper reads were slightly slower than direct NFS (**0.91x**), so uncommitted dirty data is not a read-performance win.
- Dirty writes through `fuse-overlayfs` to an NFS upper were slow: **404.5 files/s / 12.64 MiB/s**, slower than direct NFS writes in this run.
- This supports the design goal of committing read-mostly datasets/envs to SquashFS quickly; long-lived dirty overlays should stay small.

## Caveats

- This is one run on `donbot`, not a statistically rigorous benchmark suite.
- Host page cache was not globally dropped; results are best treated as practical same-run CCC behavior, not cold-cache absolutes.
- Files were 32 KiB pseudo-images; real JPEG/PNG datasets with different sizes may shift throughput.

