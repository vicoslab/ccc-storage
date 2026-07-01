"""ccc_storage_hpc — S3 mirror / cold tier and external-HPC export/import.

Everything that crosses the CCC boundary. S3 is strictly an async mirror / cold
tier / exchange bus — never CCC live truth. Has a CCC-side face (s3 mirror, cold
recall, `hpc run`, import queue) and a standalone HPC-side client installable
without the rest of the system.

Phase-00: package skeleton + client stub. Real S3/HPC logic arrives in phase-08.
"""

__version__ = "0.0.0"
