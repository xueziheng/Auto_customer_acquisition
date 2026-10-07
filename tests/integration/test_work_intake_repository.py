"""员工工作上传 PostgreSQL 仓储保留原始提取与最终确认。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.repository import RawArtifactRecord
from artifact_store.store import RawArtifactKind, RawArtifactMeta
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.tables import EmployeeConfirmationRow, ExtractedFactRow
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId, UserId
from workflows.employee_work_intake.schemas import (
    ExtractedFact,
    ExtractionPayload,
    WorkSourceKind,
)
from workflows.employee_work_intake.service_impl import WorkIntakeServiceImpl

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
OTHER_TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0Y")
ARTIFACT = ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X")
OWNER = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")


def _payload(value: str) -> ExtractionPayload:
    return ExtractionPayload(
        facts=(
            ExtractedFact(
                fact_type="customer_statement",
                value=value,
                evidence_quote="We need 500 units",
            ),
        )
    )


@pytest.mark.asyncio
async def test_postgres_keeps_agent_and_employee_versions_separate(
    integration_session: AsyncSession,
) -> None:
    from infra.db.work_intake_uow import SqlAlchemyWorkIntakeUnitOfWork

    factory = async_sessionmaker(
        bind=integration_session.bind,
        expire_on_commit=False,
    )
    raw = RawArtifactRecord(
        RawArtifactMeta(
            TENANT,
            ARTIFACT,
            RawArtifactKind.PDF,
            "a" * 64,
            10,
            "application/pdf",
            UserId("usr_01K39P9M5D6K4A91YEQ80EJZ0X"),
            NOW,
        ),
        f"raw/{TENANT}/{ARTIFACT}",
    )
    async with SqlAlchemyArtifactUnitOfWork(factory, TENANT) as artifact_uow:
        await artifact_uow.raw.insert_if_absent(raw)
    ids = iter(
        (
            "upl_01K39P9M5D6K4A91YEQ80EJZ0X",
            "wex_01K39P9M5D6K4A91YEQ80EJZ0X",
            "wcf_01K39P9M5D6K4A91YEQ80EJZ0X",
        )
    )
    service = WorkIntakeServiceImpl(
        lambda tenant_id: SqlAlchemyWorkIntakeUnitOfWork(factory, tenant_id),
        now=lambda: NOW,
        id_generator=lambda prefix: next(ids),
    )

    upload = await service.register_upload(
        TENANT,
        ARTIFACT,
        OWNER,
        WorkSourceKind.PDF_TEXT,
        occurred_at=NOW,
        customer_timezone="Asia/Shanghai",
    )
    original = await service.record_extraction(
        TENANT,
        upload.upload_id,
        _payload("Agent extracted 500 units"),
        extracted_by="team-operations-v1",
    )
    await service.confirm(
        TENANT,
        upload.upload_id,
        OWNER,
        _payload("Employee confirmed 500 units"),
    )

    async with SqlAlchemyWorkIntakeUnitOfWork(factory, TENANT) as uow:
        persisted = await uow.work_intake.get_latest_extraction(
            TENANT, upload.upload_id
        )

    assert original.payload.facts[0].value == "Agent extracted 500 units"
    assert persisted is not None
    assert persisted.payload.facts[0].value == "Agent extracted 500 units"
    assert persisted.confirmation is not None
    assert (
        persisted.confirmation.payload.facts[0].value == "Employee confirmed 500 units"
    )

    async with factory() as mutation_session:
        with pytest.raises(DBAPIError):
            await mutation_session.execute(
                update(ExtractedFactRow)
                .where(
                    ExtractedFactRow.tenant_id == str(TENANT),
                    ExtractedFactRow.extraction_id == str(original.extraction_id),
                )
                .values(extracted_by="tampered")
            )
        await mutation_session.rollback()

        with pytest.raises(DBAPIError):
            await mutation_session.execute(
                delete(EmployeeConfirmationRow).where(
                    EmployeeConfirmationRow.tenant_id == str(TENANT),
                    EmployeeConfirmationRow.extraction_id
                    == str(original.extraction_id),
                )
            )
        await mutation_session.rollback()


@pytest.mark.asyncio
async def test_work_intake_repository_rejects_cross_tenant_reads(
    integration_session: AsyncSession,
) -> None:
    from infra.db.repositories.work_intake import WorkIntakeRepositoryImpl

    repository = WorkIntakeRepositoryImpl(integration_session, TENANT)

    with pytest.raises(TenantIsolationViolation):
        await repository.list_for_employee(OTHER_TENANT, OWNER, 20)
