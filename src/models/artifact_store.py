"""DB-backed checkpoint store: `db://model_artifacts/<key>` paths.

The worker trains, the API serves — with no shared filesystem on Railway,
checkpoints cross the boundary as ModelArtifact rows. `resolve_checkpoint`
turns a db:// path into a local cached file (content-addressed by sha256),
and passes plain filesystem paths through untouched, so existing local
checkpoints keep working everywhere.
"""

from __future__ import annotations

import hashlib
import logging
import os

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ModelArtifact

logger = logging.getLogger(__name__)

DB_SCHEME = "db://model_artifacts/"
LOCAL_CACHE_DIR = os.environ.get(
    "ARTIFACT_CACHE_DIR", "models/checkpoints/artifact_cache"
)

_DIALECT_INSERTS = {
    "postgresql": postgresql.insert,
    "sqlite": sqlite.insert,
}


def is_artifact_path(checkpoint_path: str | None) -> bool:
    return bool(checkpoint_path) and checkpoint_path.startswith(DB_SCHEME)


def artifact_key(checkpoint_path: str) -> str:
    return checkpoint_path[len(DB_SCHEME):]


async def put_checkpoint(db: AsyncSession, key: str, local_path: str) -> str:
    """Upload a local checkpoint under `key` (upsert); returns the db:// path."""
    with open(local_path, "rb") as fh:
        blob = fh.read()
    digest = hashlib.sha256(blob).hexdigest()

    dialect_name = db.bind.dialect.name
    insert_fn = _DIALECT_INSERTS.get(dialect_name)
    if insert_fn is None:
        raise NotImplementedError(f"Unsupported dialect {dialect_name!r}")
    stmt = insert_fn(ModelArtifact).values(
        key=key, data=blob, sha256=digest, size_bytes=len(blob)
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"data": stmt.excluded.data, "sha256": stmt.excluded.sha256,
              "size_bytes": stmt.excluded.size_bytes},
    )
    await db.execute(stmt)
    await db.commit()
    logger.info("Stored checkpoint artifact %s (%d bytes)", key, len(blob))
    return f"{DB_SCHEME}{key}"


async def resolve_checkpoint(db: AsyncSession, checkpoint_path: str | None) -> str | None:
    """db:// path → local cached file path; anything else passes through."""
    if not is_artifact_path(checkpoint_path):
        return checkpoint_path

    key = artifact_key(checkpoint_path)
    result = await db.execute(select(ModelArtifact).where(ModelArtifact.key == key))
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise FileNotFoundError(f"Model artifact {key!r} not found in database")

    os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
    # Content-addressed cache file: a re-uploaded key gets a fresh local path,
    # which also keeps the detector LRU from serving a stale instance.
    local_path = os.path.join(
        LOCAL_CACHE_DIR, f"{key.replace('/', '__')}.{artifact.sha256[:12]}.pt"
    )
    if not os.path.exists(local_path):
        tmp_path = f"{local_path}.tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(artifact.data)
        os.replace(tmp_path, local_path)
        logger.info("Cached artifact %s -> %s", key, local_path)
    return local_path
