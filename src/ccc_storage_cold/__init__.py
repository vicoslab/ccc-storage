"""CCC cold-storage subsystem.

Cold storage is a generic pack archive/mirror layer. S3-compatible object
storage is the first backend, not the core concept.
"""

from ccc_storage_cold.archive import (  # noqa: F401
    ColdArchiveResult,
    MirrorResult,
    RecallError,
    archive_committed_packs_to_cold_storage,
    mirror_committed_packs_to_cold_storage,
    recall_cold_storage_packs,
)
from ccc_storage_cold.config import ColdStorageConfig  # noqa: F401
from ccc_storage_cold.object_store import (  # noqa: F401
    Boto3ObjectStore,
    LocalObjectStore,
    ObjectStore,
    ObjectStoreError,
    S3Config,
)
