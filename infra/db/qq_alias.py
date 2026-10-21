"""别名仅从既有 Gateway 的成功头证据入账；无自动发现或人工猜测入口。"""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.qq_mail.sent_alias import SentAliasEvidence, verify_sent_alias
from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialEventRow, QQTrialRow
from infra.db.repositories.email_inbound import lock_tenant
from infra.db.tables import (
    EmailInboundCursorRow,
    OutreachMessageAttemptRow,
    RawArtifactRow,
    ToolCallRow,
)
from shared.schemas.identifiers import new_id
from shared.schemas.qq_trial import QQTrialScope, QQTrialStopped


def collection_binding(scope: QQTrialScope) -> tuple[str, str]:
    """来源与真实读取员工绑定，手工导出不在可采集来源中。"""
    if scope.alias_source == "controlled_recipient_imap":
        if not scope.delivery_user_id or not scope.delivery_employee_id:
            raise QQTrialStopped("alias_binding")
        return "email.qq.delivery_alias.read", scope.delivery_user_id
    return "email.qq.sent_alias.read", scope.user_id


async def record_verified_alias(
    factory: async_sessionmaker[AsyncSession],
    scope: QQTrialScope,
    evidence: SentAliasEvidence,
    *,
    artifact_id: str,
    tool_call_id: str,
    authorize: Callable[[], Awaitable[bool]],
) -> None:
    """等待真实只读 alias 工具及原件来源；未注册工具不能靠本函数获得启用权。"""
    if evidence.source not in {"sender_sent", "controlled_recipient_imap"}:
        raise QQTrialStopped("alias_source_not_collectable")
    if evidence.source != scope.alias_source:
        raise QQTrialStopped("alias_binding")
    expected_tool, expected_user = collection_binding(scope)
    if not await authorize() or (
        evidence.provider_header,
        evidence.canonical_header,
        evidence.sender,
        evidence.recipient,
    ) != (scope.provider_header, scope.canonical_header, scope.sender, scope.recipient):
        raise QQTrialStopped("alias_binding")
    tenant = scope.route.tenant_id
    async with factory.begin() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": tenant},
        )
        call = await session.scalar(
            select(ToolCallRow).where(
                ToolCallRow.tenant_id == tenant,
                ToolCallRow.tool_call_id == tool_call_id,
                ToolCallRow.tool_id == expected_tool,
                ToolCallRow.status == "succeeded",
                ToolCallRow.message_attempt_id == scope.attempt_id,
                ToolCallRow.user_id == expected_user,
                ToolCallRow.provider_ref == artifact_id,
            )
        )
        artifact = await session.scalar(
            select(RawArtifactRow).where(
                RawArtifactRow.tenant_id == tenant,
                RawArtifactRow.artifact_id == artifact_id,
                RawArtifactRow.content_hash == evidence.headers_sha256,
                RawArtifactRow.size_bytes == len(evidence.raw_headers),
            )
        )
        if (
            call is None
            or (scope.alias_source == "controlled_recipient_imap" and call.tool_version != "v1")
            or artifact is None
            or hashlib.sha256(evidence.raw_headers).hexdigest()
            != evidence.headers_sha256
        ):
            raise QQTrialStopped("alias_provenance_missing")
        attempt = await session.scalar(
            select(OutreachMessageAttemptRow)
            .where(
                OutreachMessageAttemptRow.tenant_id == tenant,
                OutreachMessageAttemptRow.attempt_id == scope.attempt_id,
                OutreachMessageAttemptRow.state == "sent",
                OutreachMessageAttemptRow.deterministic_message_id
                == scope.canonical_header,
                OutreachMessageAttemptRow.sending_identity_id
                == scope.route.configured_identity_id,
            )
            .with_for_update()
        )
        if (
            attempt is None
            or not attempt.idempotency_header
            or not attempt.provider_ref
        ):
            raise QQTrialStopped("attempt_changed")
        verified = verify_sent_alias(
            (evidence.raw_headers,),
            source=evidence.source,
            canonical_header=scope.canonical_header,
            idempotency_header=attempt.idempotency_header,
            sender=scope.sender,
            recipient=scope.recipient,
        )
        if verified != evidence or not await authorize():
            raise QQTrialStopped("alias_binding")
        old = await session.scalar(
            select(QQSentAliasRow).where(
                QQSentAliasRow.tenant_id == tenant,
                QQSentAliasRow.attempt_id == scope.attempt_id,
            )
        )
        if old is not None:
            if (
                old.provider_header,
                old.canonical_header,
                old.artifact_id,
                old.tool_call_id,
            ) != (
                evidence.provider_header,
                evidence.canonical_header,
                artifact_id,
                tool_call_id,
            ):
                raise QQTrialStopped("alias_conflict")
            return
        session.add(
            QQSentAliasRow(
                tenant_id=tenant,
                evidence_id=scope.alias_evidence_id,
                identity_id=scope.route.configured_identity_id,
                attempt_id=scope.attempt_id,
                alias_digest=hashlib.sha256(scope.provider_header.encode()).hexdigest(),
                provider_header=evidence.provider_header,
                canonical_header=evidence.canonical_header,
                artifact_id=artifact_id,
                artifact_hash=evidence.headers_sha256,
                tool_call_id=tool_call_id,
                created_at=await session.scalar(select(func.clock_timestamp())),
            )
        )


async def provision_trial(
    factory: async_sessionmaker[AsyncSession],
    scope: QQTrialScope,
    authorize: Callable[[], Awaitable[bool]],
    *, preparation_collected: bool = False,
) -> None:
    """受信确认流程的存储端口；不提供网页/脚本启用入口，不复用旧 attempt 预算。"""
    expected_tool, expected_user = collection_binding(scope)
    if not await authorize():
        raise QQTrialStopped("scope_changed")
    async with factory.begin() as session:
        tenant = scope.route.tenant_id
        await lock_tenant(session, tenant)
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": tenant},
        )
        alias = await session.scalar(
            select(QQSentAliasRow).where(
                QQSentAliasRow.tenant_id == tenant,
                QQSentAliasRow.evidence_id == scope.alias_evidence_id,
                QQSentAliasRow.attempt_id == scope.attempt_id,
                QQSentAliasRow.identity_id == scope.route.configured_identity_id,
                QQSentAliasRow.provider_header == scope.provider_header,
                QQSentAliasRow.canonical_header == scope.canonical_header,
            )
        )
        stamp = await session.scalar(select(func.clock_timestamp()))
        existing_cursor = await session.scalar(
            select(EmailInboundCursorRow).where(
                EmailInboundCursorRow.tenant_id == tenant,
                EmailInboundCursorRow.mailbox_alias == scope.route.mailbox_alias,
            )
        )
        if existing_cursor is not None:
            raise QQTrialStopped("existing_cursor_forbidden")
        if (
            alias is None
            or not isinstance(stamp, datetime)
            or stamp >= scope.expires_at
        ):
            raise QQTrialStopped("alias_or_authorization_missing")
        if preparation_collected:
            collected = await session.scalar(select(ToolCallRow).where(
                ToolCallRow.tenant_id == tenant,
                ToolCallRow.tool_call_id == alias.tool_call_id,
                ToolCallRow.tool_id == expected_tool,
                ToolCallRow.tool_version == "v1",
                ToolCallRow.user_id == expected_user,
                ToolCallRow.status == "succeeded",
                ToolCallRow.attempt_count == 1,
                ToolCallRow.message_attempt_id == scope.attempt_id,
                ToolCallRow.provider_ref == alias.artifact_id,
            ))
            if collected is None or min(scope.limits.sessions,scope.limits.searches,scope.limits.headers)<2:
                raise QQTrialStopped("alias_collection_budget_missing")
        session.add(
            QQTrialRow(
                tenant_id=tenant,
                trial_id=scope.trial_id,
                mailbox_alias=scope.route.mailbox_alias,
                attempt_id=scope.attempt_id,
                alias_evidence_id=scope.alias_evidence_id,
                scope=scope.model_dump(mode="json"),
                state="active",
                sessions=int(preparation_collected),
                searches=int(preparation_collected),
                headers=int(preparation_collected),
            )
        )
        if preparation_collected:
            await session.flush()
            session.add(QQTrialEventRow(tenant_id=tenant,event_id=new_id("qte"),
                trial_id=scope.trial_id,kind=("delivery_collection_reserved" if scope.alias_source == "controlled_recipient_imap" else "sent_collection_reserved"),created_at=stamp,input_bytes=0))
