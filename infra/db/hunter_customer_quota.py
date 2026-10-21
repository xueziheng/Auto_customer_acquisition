"""Hunter 客户验证的tenant持久技术预算，不保存联系人或Provider原始内容。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import ToolCallEventRow, ToolCallRow
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, new_id
from tool_gateway.checks.contact_provider import ContactVerificationPreflight
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState

_BUDGET_RULE = "hunter-customer-budget-v1"


class PostgresHunterCustomerQuota:
    """接收当前Gateway行的配额stage；received也是保守预占，不退款未知执行。"""

    name = "rate_limit"

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        maximum_daily_verifications: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(maximum_daily_verifications) is not int
            or maximum_daily_verifications < 1
            or not tenant_id
        ):
            raise ValidationError("Hunter 客户验证预算无效")
        self._factory, self._tenant, self._maximum, self._now = (
            factory,
            tenant_id,
            maximum_daily_verifications,
            now,
        )

    async def reserve(
        self, tenant_id: TenantId, capability: str, now: datetime
    ) -> int | None:
        """旧无调用ID接口不能证明持久预占；装配缺少具名stage时必须关闭。"""
        raise ValidationError("Hunter 持久预算必须绑定当前工具调用")

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if ctx.tool_id != "contact.verify":
            return CheckRejection(
                self.name,
                "rate_limit:customer_enrichment_disabled",
                "此入口未启用联系人补全",
            )
        preflight = state.preflight
        now = self._now()
        if (
            ctx.tenant_id != self._tenant
            or not isinstance(preflight, ContactVerificationPreflight)
            or preflight.tenant_id != self._tenant
            or state.manifest.tool_id != ctx.tool_id
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
        ):
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            async with self._factory() as session, session.begin():
                lock = int.from_bytes(
                    sha256(
                        f"hunter-customer-budget-v1:{self._tenant}".encode()
                    ).digest()[:8],
                    "big",
                    signed=True,
                )
                await session.execute(select(func.pg_advisory_xact_lock(lock)))
                row = await session.scalar(
                    select(ToolCallRow)
                    .where(
                        ToolCallRow.tenant_id == self._tenant,
                        ToolCallRow.tool_call_id == state.tool_call_id,
                    )
                    .with_for_update()
                )
                if (
                    row is None
                    or row.tool_id != "contact.verify"
                    or row.user_id != ctx.user_id
                    or row.status != "received"
                    or not day <= row.created_at < day + timedelta(days=1)
                ):
                    raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
                previous = await session.scalar(
                    select(ToolCallEventRow.outcome).where(
                        ToolCallEventRow.tenant_id == self._tenant,
                        ToolCallEventRow.tool_call_id == row.tool_call_id,
                        ToolCallEventRow.rule == _BUDGET_RULE,
                    )
                )
                outcome = "cache_hit" if preflight.cache_valid else "reservation"
                if previous is not None:
                    if previous != outcome:
                        raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
                    return None
                if not preflight.cache_valid:
                    cached = exists(
                        select(ToolCallEventRow.event_id).where(
                            ToolCallEventRow.tenant_id == self._tenant,
                            ToolCallEventRow.tool_call_id == ToolCallRow.tool_call_id,
                            ToolCallEventRow.rule == _BUDGET_RULE,
                            ToolCallEventRow.outcome == "cache_hit",
                        )
                    )
                    used, second_used, minute_used = (
                        await session.execute(
                            select(
                                func.count().filter(ToolCallRow.created_at >= day),
                                func.count().filter(
                                    ToolCallRow.created_at > now - timedelta(seconds=1)
                                ),
                                func.count().filter(
                                    ToolCallRow.created_at > now - timedelta(minutes=1)
                                ),
                            )
                            .select_from(ToolCallRow)
                            .where(
                                ToolCallRow.tenant_id == self._tenant,
                                ToolCallRow.tool_id == "contact.verify",
                                ToolCallRow.created_at
                                >= min(day, now - timedelta(minutes=1)),
                                ToolCallRow.created_at < day + timedelta(days=1),
                                ToolCallRow.status.notin_(("rejected", "duplicate")),
                                ~cached,
                            )
                        )
                    ).one()
                    # 本次received已提交且计入，等于上限仍可执行；拒绝/duplicate永不收费。
                    if used is None or used > self._maximum:
                        raise ToolGatewayError(
                            ToolErrorCategory.RATE_LIMITED,
                            retry_after_seconds=max(
                                1,
                                min(
                                    86400,
                                    int(
                                        (day + timedelta(days=1) - now).total_seconds()
                                    ),
                                ),
                            ),
                        )
                    if minute_used > 300 or second_used > 10:
                        raise ToolGatewayError(
                            ToolErrorCategory.RATE_LIMITED,
                            retry_after_seconds=60 if minute_used > 300 else 1,
                        )
                session.add(
                    ToolCallEventRow(
                        tenant_id=self._tenant,
                        event_id=new_id("tce"),
                        tool_call_id=row.tool_call_id,
                        stage="rate_limit",
                        outcome=outcome,
                        rule=_BUDGET_RULE,
                        category=None,
                        actor_id=ctx.user_id,
                        run_id=ctx.run_id,
                        campaign_id=ctx.campaign_ref,
                        message_attempt_id=None,
                        occurred_at=now,
                        duration_ms=0,
                        cost_note="provider_cache_hit"
                        if preflight.cache_valid
                        else "provider_budget_reserved",
                    )
                )
        except ToolGatewayError:
            raise
        except Exception:  # noqa: BLE001 - 提交/关闭未知不放行，不暴露存储异常
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
        return None
