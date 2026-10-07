"""用真实 PostgreSQL + S3/MinIO 演示 Artifact Store；仅供离线验收。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from artifact_store.repository import ArtifactUnitOfWork
from artifact_store.service_impl import (
    GeneratedArtifactStoreImpl,
    RawArtifactStoreImpl,
)
from artifact_store.store import GeneratedArtifactKind, RawArtifactKind
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import S3ObjectBlobTransport
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tables import GeneratedArtifactRow, RawArtifactRow
from infra.secrets import EnvironmentSecretResolver
from shared.schemas.identifiers import IdempotencyKey, RunId, TenantId, new_id

_FAILURE = "Artifact Store 演示运行失败"
_RAW_CONTENT = b"artifact-demo-raw-content-marker"
_GENERATED_CONTENT = b"artifact-demo-generated-content-marker"
_GENERATED_MIME = "application/vnd.tradeos.email-draft+json"


async def _row_counts(
    factory: async_sessionmaker[AsyncSession], tenant_id: TenantId
) -> tuple[int, int]:
    async with factory() as session:
        raw_count = await session.scalar(
            select(func.count()).select_from(RawArtifactRow).where(
                RawArtifactRow.tenant_id == str(tenant_id)
            )
        )
        generated_count = await session.scalar(
            select(func.count()).select_from(GeneratedArtifactRow).where(
                GeneratedArtifactRow.tenant_id == str(tenant_id)
            )
        )
    return int(raw_count or 0), int(generated_count or 0)


async def run_demo(environ: Mapping[str, str]) -> dict[str, object]:
    """装配真实 adapters 并返回安全摘要；不打印配置、对象键或内容。"""
    settings = S3ObjectStoreSettings.from_environ(environ)
    engine: AsyncEngine | None = None
    primary: BaseException | None = None
    try:
        engine = create_engine_from(environ["DATABASE_URL"])
        factory = async_sessionmaker(engine, expire_on_commit=False)

        def uow_factory(tenant_id: TenantId) -> ArtifactUnitOfWork:
            return cast(
                ArtifactUnitOfWork,
                SqlAlchemyArtifactUnitOfWork(factory, tenant_id),
            )

        transport = S3ObjectBlobTransport(
            settings, EnvironmentSecretResolver(environ)
        )
        raw_store = RawArtifactStoreImpl(
            uow_factory,
            transport,
            settings.raw_max_bytes,
            lambda: _utc_now(),
            new_id,
        )
        generated_store = GeneratedArtifactStoreImpl(
            uow_factory,
            transport,
            settings.generated_max_bytes,
            lambda: _utc_now(),
            new_id,
        )
        tenant_id = TenantId(new_id("tn"))
        raw_first = await raw_store.put(
            tenant_id,
            RawArtifactKind.PDF,
            _RAW_CONTENT,
            "application/pdf",
        )
        raw_second = await raw_store.put(
            tenant_id,
            RawArtifactKind.PDF,
            _RAW_CONTENT,
            "application/pdf",
        )
        subject_ref = f"enr_{str(new_id('enr')).split('_', 1)[1]}"
        key = IdempotencyKey(f"{subject_ref}:1:draft")
        workflow_run_id = RunId(new_id("run"))
        generated_first = await generated_store.put(
            tenant_id,
            GeneratedArtifactKind.EMAIL_DRAFT,
            _GENERATED_CONTENT,
            _GENERATED_MIME,
            workflow_run_id=workflow_run_id,
            subject_ref=subject_ref,
            sequence_number=1,
            idempotency_key=key,
            generated_by="outreach_agent_v1",
        )
        generated_second = await generated_store.put(
            tenant_id,
            GeneratedArtifactKind.EMAIL_DRAFT,
            _GENERATED_CONTENT,
            _GENERATED_MIME,
            workflow_run_id=workflow_run_id,
            subject_ref=subject_ref,
            sequence_number=1,
            idempotency_key=key,
            generated_by="outreach_agent_v1",
        )
        if raw_first != raw_second or generated_first != generated_second:
            raise RuntimeError("Artifact demo idempotency invariant failed")
        if await raw_store.get(tenant_id, raw_first.artifact_id) != (
            raw_first,
            _RAW_CONTENT,
        ):
            raise RuntimeError("Artifact demo raw readback failed")
        if await generated_store.get(
            tenant_id, generated_first.artifact_id
        ) != (generated_first, _GENERATED_CONTENT):
            raise RuntimeError("Artifact demo generated readback failed")
        raw_count, generated_count = await _row_counts(factory, tenant_id)
        if raw_count != 1 or generated_count != 1:
            raise RuntimeError("Artifact demo metadata count failed")
        return {
            "tenant_id": str(tenant_id),
            "raw_artifact_id": str(raw_first.artifact_id),
            "generated_artifact_id": str(generated_first.artifact_id),
            "raw_hash": raw_first.content_hash,
            "generated_hash": generated_first.content_hash,
            "raw_put_count": 2,
            "raw_row_count": raw_count,
            "generated_put_count": 2,
            "generated_row_count": generated_count,
        }
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except BaseException:
                if primary is None:
                    raise


def _utc_now() -> datetime:
    return datetime.now(UTC)


def main() -> int:
    """进程边界只输出单行安全 JSON 或固定失败消息。"""
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(run_demo(dict(os.environ)))
    except Exception:  # noqa: BLE001 - 进程边界只能输出固定脱敏消息
        sys.stderr.write(f"{_FAILURE}\n")
        return 1
    sys.stdout.write(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
