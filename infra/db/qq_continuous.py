"""已验收私有 QQ 会话持续读取；每页重查当前范围和不可变采集来源。"""
from dataclasses import replace

from sqlalchemy import select

from connectors.qq_mail.inbound import ProjectReplyGrant, ReplyHeaders
from infra.db.qq_alias import collection_binding
from infra.db.qq_inbound import PostgresProjectReplyScope
from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialRow
from infra.db.tables import RawArtifactRow, ToolCallRow
from shared.schemas.email_inbound import InboundError, InboundRoute
from shared.schemas.identifiers import EmployeeId
from shared.schemas.qq_trial import QQTrialScope


class PostgresContinuousReplyScope:
    def __init__(self, factory, base: PostgresProjectReplyScope, settings):
        self.factory, self.base, self.settings = factory, base, settings

    async def target(self, route: InboundRoute, employee_id: EmployeeId) -> ProjectReplyGrant | None:
        config = self.settings.continuous
        if config is None or route != config.route:
            raise InboundError()
        grant = await self.base.target(route, employee_id)
        if grant is None:
            return None
        tenant = route.tenant_id
        async with self.factory() as session:
            trial = await session.scalar(select(QQTrialRow).where(
                QQTrialRow.tenant_id == tenant, QQTrialRow.trial_id == config.source_trial_id,
                QQTrialRow.state == "stopped", QQTrialRow.mailbox_alias == route.mailbox_alias,
                QQTrialRow.attempt_id == grant.attempt_id,
                QQTrialRow.alias_evidence_id == config.alias_evidence_id,
            ))
            if trial is None:
                raise InboundError()
            scope = QQTrialScope.model_validate(trial.scope)
            if (scope.route != route or scope.employee_id != employee_id
                or scope.user_id != self.settings.user_id
                or scope.classifier_employee_id != self.settings.classifier_employee_id
                or scope.canonical_header != grant.outbound_message_header
                or (scope.sender, scope.recipient) != (grant.sender, grant.recipient)):
                raise InboundError()
            tool, user = collection_binding(scope)
            rows = (await session.execute(select(QQSentAliasRow)
                .join(ToolCallRow,
                    (ToolCallRow.tenant_id == tenant)
                    & (ToolCallRow.tool_call_id == QQSentAliasRow.tool_call_id)
                    & (ToolCallRow.tool_id == tool) & (ToolCallRow.user_id == user)
                    & (ToolCallRow.tool_version == "v1") & (ToolCallRow.status == "succeeded")
                    & (ToolCallRow.message_attempt_id == grant.attempt_id)
                    & (ToolCallRow.provider_ref == QQSentAliasRow.artifact_id))
                .join(RawArtifactRow,
                    (RawArtifactRow.tenant_id == tenant)
                    & (RawArtifactRow.artifact_id == QQSentAliasRow.artifact_id)
                    & (RawArtifactRow.content_hash == QQSentAliasRow.artifact_hash))
                .where(QQSentAliasRow.tenant_id == tenant,
                    QQSentAliasRow.evidence_id == config.alias_evidence_id,
                    QQSentAliasRow.identity_id == route.configured_identity_id,
                    QQSentAliasRow.attempt_id == grant.attempt_id,
                    QQSentAliasRow.canonical_header == grant.outbound_message_header,
                    QQSentAliasRow.provider_header == scope.provider_header)
                .limit(2))).scalars().all()
            if len(rows) != 1:
                raise InboundError()
            return replace(grant, provider_header=rows[0].provider_header)

    async def resolve(self, route: InboundRoute, employee_id: EmployeeId, headers: ReplyHeaders):
        grant = await self.target(route, employee_id)
        if grant is None:
            return None
        return grant if (
            headers.in_reply_to == grant.outbound_message_header
            and (headers.sender, headers.recipient) == (grant.recipient, grant.sender)
        ) else None
