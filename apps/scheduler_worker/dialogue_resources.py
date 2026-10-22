"""正式普通对话借用原对象存储；不伪造冷收信路由或读取邮箱凭证。"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.repository import ArtifactUnitOfWork
from artifact_store.service_impl import GeneratedArtifactStoreImpl, RawArtifactStoreImpl
from connectors.gmail.client import SecretResolver
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from shared.schemas.email_inbound import MIME_BYTES
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import new_id


@dataclass(frozen=True)
class DialogueArtifactPorts:
    object_settings: S3ObjectStoreSettings
    resolver: SecretResolver


@dataclass(frozen=True)
class DialogueArtifacts:
    raw: RawArtifactStoreImpl
    generated: GeneratedArtifactStoreImpl
    objects: DeferredS3ObjectBlobTransport

    async def aclose(self) -> None:
        await self.objects.aclose()


def build_dialogue_artifacts(ports: DialogueArtifactPorts,
    sessions: async_sessionmaker[AsyncSession], now: Callable[[], datetime]) -> DialogueArtifacts:
    objects = DeferredS3ObjectBlobTransport(ports.object_settings, ports.resolver)
    bounded = S3BoundedObjectBlobTransport(ports.object_settings, ports.resolver,
        limits=ObjectReadLimits(connect_timeout_ms=2000, read_timeout_ms=2000,
            total_timeout_ms=10000, maximum_attempts=1, chunk_bytes=65536))
    factory = lambda tenant: cast(ArtifactUnitOfWork, SqlAlchemyArtifactUnitOfWork(sessions, tenant))
    return DialogueArtifacts(
        RawArtifactStoreImpl(factory, objects, MIME_BYTES, now, new_id, bounded_transport=bounded),
        GeneratedArtifactStoreImpl(factory, objects, 32768, now, new_id, bounded_transport=bounded), objects)
