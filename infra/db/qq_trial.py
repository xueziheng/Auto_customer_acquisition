"""受控验收的行锁、不可退还预留和当前授权；不接触邮箱或模型。"""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialEventRow, QQTrialRow
from infra.db.tables import (
    EmployeeRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
)
from shared.schemas.identifiers import new_id
from shared.schemas.qq_trial import (
    BudgetKind,
    QQTrialScope,
    QQTrialStopped,
    QQTrialWatermark,
)


class SqlQQTrialGate:
    """每个 dispatch 先独立提交预算；业务提交借用同一事务锁定范围。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        scope: QQTrialScope | None,
        authorize: Callable[[], Awaitable[bool]],
    ) -> None:
        self.factory, self._scope, self.authorize = factory, scope, authorize
        self._scan: ContextVar[str | None] = ContextVar("qq_trial_scan", default=None)

    @property
    def scope(self) -> QQTrialScope:
        """锁后采集成功前失败关闭；不以配置猜测服务商邮件编号。"""
        if self._scope is None:
            raise QQTrialStopped("trial_not_activated")
        return self._scope

    @scope.setter
    def scope(self, scope: QQTrialScope) -> None:
        if self._scope is not None:
            raise QQTrialStopped("runtime_already_bound")
        self._scope = scope

    def bind_scope(self, scope: QQTrialScope) -> None:
        """同一运行实例仅可绑定一次真实采集并登记的范围。"""
        self.scope = scope

    async def _current(self) -> None:
        if not await self.authorize():
            raise QQTrialStopped("scope_changed")

    async def _row(
        self, session: AsyncSession, *, active: bool = True
    ) -> tuple[QQTrialRow, datetime]:
        tenant = self.scope.route.tenant_id
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": tenant},
        )
        row = await session.scalar(
            select(QQTrialRow)
            .where(
                QQTrialRow.tenant_id == tenant,
                QQTrialRow.trial_id == self.scope.trial_id,
            )
            .with_for_update()
        )
        stamp = await session.scalar(select(func.clock_timestamp()))
        if row is None or not isinstance(stamp, datetime):
            raise QQTrialStopped("trial_missing")
        if QQTrialScope.model_validate(row.scope) != self.scope:
            raise QQTrialStopped("scope_changed")
        if active and (
            row.state not in {"active", "received"}
            or not self.scope.starts_at <= stamp < self.scope.expires_at
        ):
            raise QQTrialStopped("expired_or_stopped")
        alias = await session.scalar(
            select(QQSentAliasRow).where(
                QQSentAliasRow.tenant_id == tenant,
                QQSentAliasRow.evidence_id == self.scope.alias_evidence_id,
                QQSentAliasRow.identity_id == self.scope.route.configured_identity_id,
                QQSentAliasRow.attempt_id == self.scope.attempt_id,
            )
        )
        if alias is None or (alias.provider_header, alias.canonical_header) != (
            self.scope.provider_header,
            self.scope.canonical_header,
        ):
            raise QQTrialStopped("alias_missing_or_changed")
        if active:
            attempt = await session.scalar(
                select(OutreachMessageAttemptRow)
                .where(
                    OutreachMessageAttemptRow.tenant_id == tenant,
                    OutreachMessageAttemptRow.attempt_id == self.scope.attempt_id,
                    OutreachMessageAttemptRow.state == "sent",
                    OutreachMessageAttemptRow.sending_identity_id
                    == self.scope.route.configured_identity_id,
                    OutreachMessageAttemptRow.deterministic_message_id
                    == self.scope.canonical_header,
                )
                .with_for_update()
            )
            if attempt is None or not attempt.provider_ref:
                raise QQTrialStopped("attempt_changed")
            enrollment = await session.scalar(
                select(OutreachEnrollmentRow).where(
                    OutreachEnrollmentRow.tenant_id == tenant,
                    OutreachEnrollmentRow.enrollment_id == attempt.enrollment_id,
                    OutreachEnrollmentRow.execution_mode == "manual",
                    OutreachEnrollmentRow.source_hypothesis_id.is_(None),
                )
            )
            if enrollment is None:
                raise QQTrialStopped("attempt_changed")
            owner = await session.scalar(
                select(OwnershipLockRow)
                .where(
                    OwnershipLockRow.tenant_id == tenant,
                    OwnershipLockRow.account_id == enrollment.account_id,
                )
                .with_for_update()
            )
            employees = (
                await session.scalars(
                    select(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == tenant,
                        EmployeeRow.employee_id.in_(
                            (self.scope.employee_id, self.scope.classifier_employee_id)
                        ),
                    )
                    .order_by(EmployeeRow.employee_id)
                    .with_for_update()
                )
            ).all()
            facts = {e.employee_id: e for e in employees}
            mailbox = facts.get(self.scope.employee_id)
            classifier = facts.get(self.scope.classifier_employee_id)
            if (
                owner is None
                or owner.owner != self.scope.employee_id
                or mailbox is None
                or classifier is None
                or not mailbox.is_active
                or not classifier.is_active
                or mailbox.user_id != self.scope.user_id
                or classifier.user_id != self.scope.classifier_user_id
                or classifier.role != "boss"
            ):
                raise QQTrialStopped("identity_or_owner_changed")
            await self._current()
            stamp = await session.scalar(select(func.clock_timestamp()))
            if (
                not isinstance(stamp, datetime)
                or not self.scope.starts_at <= stamp < self.scope.expires_at
            ):
                raise QQTrialStopped("expired_or_stopped")
        return row, stamp

    def _event(
        self, session: AsyncSession, kind: str, stamp: datetime, size: int = 0
    ) -> None:
        session.add(
            QQTrialEventRow(
                tenant_id=self.scope.route.tenant_id,
                event_id=new_id("qte"),
                trial_id=self.scope.trial_id,
                kind=kind,
                created_at=stamp,
                input_bytes=size,
            )
        )

    async def check(self) -> None:
        await self._current()
        async with self.factory.begin() as session:
            await self._row(session)

    async def claim_scan(self) -> bool:
        await self._current()
        token = new_id("qsc")
        async with self.factory.begin() as session:
            row, stamp = await self._row(session)
            if row.state == "received":
                return False
            if row.scan_token is not None:
                raise QQTrialStopped("scan_result_unknown")
            if row.next_scan_at is not None and stamp < row.next_scan_at:
                return False
            row.scan_token = token
            row.next_scan_at = stamp + timedelta(
                seconds=self.scope.limits.interval_seconds
            )
            self._event(session, "scan_claimed", stamp)
        self._scan.set(token)
        return True

    async def watermark(self) -> QQTrialWatermark | None:
        await self._current()
        async with self.factory.begin() as session:
            row, _ = await self._row(session)
            return (
                QQTrialWatermark.model_validate(row.watermark)
                if row.watermark
                else None
            )

    async def initialize(self, value: QQTrialWatermark) -> None:
        await self._current()
        async with self.factory.begin() as session:
            row, stamp = await self._row(session)
            if (
                row.scan_token != self._scan.get()
                or row.scan_token is None
                or row.watermark is not None
            ):
                raise QQTrialStopped("watermark_conflict")
            row.watermark = value.model_dump()
            self._event(session, "watermark_recorded", stamp)

    async def reserve(self, kind: BudgetKind, *, input_bytes: int = 0) -> None:
        await self._current()
        if (
            kind
            not in {
                "sessions",
                "searches",
                "headers",
                "bodies",
                "classifications",
                "provider_requests",
            }
            or type(input_bytes) is not int
            or input_bytes < 0
        ):
            raise QQTrialStopped("invalid_budget")
        async with self.factory.begin() as session:
            row, stamp = await self._row(session)
            if kind in {"sessions", "searches", "headers", "bodies"}:
                if (
                    row.state != "active"
                    or row.scan_token != self._scan.get()
                    or row.scan_token is None
                ):
                    raise QQTrialStopped("scan_binding")
            elif row.state != "received" or not row.message_id:
                raise QQTrialStopped("message_binding")
            if kind == "provider_requests" and (
                row.classifications != 1
                or row.model_started_at is None
                or stamp
                >= row.model_started_at
                + timedelta(seconds=self.scope.limits.model_timeout_seconds)
            ):
                raise QQTrialStopped("model_expired")
            if kind == "provider_requests" and (
                row.provider_pending
                or row.output_tokens >= self.scope.limits.model_output_tokens
            ):
                raise QQTrialStopped("provider_unknown_or_exhausted")
            if getattr(row, kind) >= getattr(self.scope.limits, kind):
                raise QQTrialStopped("budget_exhausted")
            if row.input_bytes + input_bytes > self.scope.limits.model_input_bytes:
                raise QQTrialStopped("model_input_limit")
            setattr(row, kind, getattr(row, kind) + 1)
            row.input_bytes += input_bytes
            if kind == "classifications":
                row.model_started_at = stamp
            if kind == "provider_requests":
                row.provider_pending = True
            self._event(session, kind, stamp, input_bytes)

    async def remaining_output(self) -> int:
        async with self.factory.begin() as session:
            row, _ = await self._row(session)
            return self.scope.limits.model_output_tokens - row.output_tokens

    async def provider_finished(self, tokens: int, requested: int) -> None:
        async with self.factory.begin() as session:
            row, stamp = await self._row(session, active=False)
            if (
                not row.provider_pending
                or type(tokens) is not int
                or not 0 <= tokens <= requested
                or row.output_tokens + tokens > self.scope.limits.model_output_tokens
            ):
                raise QQTrialStopped("provider_usage_unknown")
            row.output_tokens += tokens
            row.provider_pending = False
            self._event(session, "provider_finished", stamp)
        await self.check()

    async def require_message(self, message_id: str) -> None:
        await self._current()
        async with self.factory.begin() as session:
            row, _ = await self._row(session)
            if row.message_id != message_id or row.state != "received":
                raise QQTrialStopped("message_binding")

    async def commit_message(
        self, session: AsyncSession, message_id: str | None
    ) -> None:
        """复用分类/工作项的原事务；行锁覆盖最终当前归属检查到提交。"""
        row, _ = await self._row(session)
        if (
            message_id is None
            or row.message_id != message_id
            or row.state != "received"
        ):
            raise QQTrialStopped("message_binding")

    async def commit_scan(self, session: AsyncSession, message_id: str | None) -> None:
        """仅由原入站页 UoW 调用；同事务提交消息、收据、游标与扫描状态。"""
        await self._current()
        row, stamp = await self._row(session)
        if (
            row.scan_token is None
            or row.scan_token != self._scan.get()
            or row.state != "active"
        ):
            raise QQTrialStopped("scan_binding")
        if message_id is not None:
            if row.bodies != 1 or row.message_id is not None:
                raise QQTrialStopped("message_conflict")
            row.message_id, row.state = message_id, "received"
        elif row.bodies:
            raise QQTrialStopped("body_not_committed")
        row.scan_token = None
        self._event(session, "page_committed", stamp)

    async def record_address_compatibility(self, fields: tuple[str, ...]) -> None:
        """记录双阶段复核所用固定规则；不记录原地址，不代表原件已归档/消息已提交。"""
        if fields not in {("from",), ("to",), ("from", "to")}:
            raise QQTrialStopped("address_audit_invalid")
        await self._current()
        async with self.factory.begin() as session:
            row, stamp = await self._row(session)
            if (row.state != "active" or row.bodies != 1 or row.scan_token is None
                    or row.scan_token != self._scan.get()):
                raise QQTrialStopped("scan_binding")
            kinds = tuple(f"address_{field}_quoted_utf8_b_v1" for field in fields)
            previous = (await session.scalars(select(QQTrialEventRow.kind).where(
                QQTrialEventRow.tenant_id == self.scope.route.tenant_id,
                QQTrialEventRow.trial_id == self.scope.trial_id,
                QQTrialEventRow.kind.in_(kinds),
            ))).all()
            for kind in kinds:
                if kind not in previous:
                    self._event(session, kind, stamp)

    async def stop(self, reason: str) -> None:
        # 原始异常不进入审计；停止不能退款、清水位或重开新版本。
        async with self.factory.begin() as session:
            tenant = self.scope.route.tenant_id
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": tenant},
            )
            row = await session.scalar(
                select(QQTrialRow)
                .where(
                    QQTrialRow.tenant_id == tenant,
                    QQTrialRow.trial_id == self.scope.trial_id,
                )
                .with_for_update()
            )
            if row is None or row.state in {"stopped", "complete"}:
                return
            stamp = await session.scalar(select(func.clock_timestamp()))
            if not isinstance(stamp, datetime):
                raise QQTrialStopped("clock_unavailable")
            row.state, row.reason = (
                "stopped",
                reason if type(reason) is str and reason in {
                    "cancelled", "mime_headers_invalid", "bcc_not_allowed",
                    "attachments_not_allowed", "mime_structure_unverified",
                    "mime_type_not_supported",
                    "reply_headers_unsafe", "mime_structure_invalid",
                    "mime_structure_limit", "mime_body_mismatch",
                } else "failed_or_unknown",
            )
            self._event(session, row.reason, stamp)
