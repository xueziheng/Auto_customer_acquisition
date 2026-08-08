"""TradeOS Slice 2 机会域演示脚本。

走查：创建机会 → 打分（分桶 + 门槛解释）→ 普通 transition(LOST) 被拒 →
mark_lost(reason=None) 被拒 → 结构化 mark_lost → 读回实体验证终态字段。

连接串**只从 ``os.environ["DATABASE_URL"]`` 读取**；不 load_dotenv、不写默认值、
不打印/记录 URL、不读其他环境变量。所有输出内部中文，无客户内容。
仅数据库动作（经 infra/UoW），不调用模型/网络 API。
"""
import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.opportunities.errors import MissingLossReasonError
from domains.opportunities.permissions import (
    Actor,
    OpportunityAction,
    OpportunityScope,
    ScopeLevel,
)
from domains.opportunities.schemas import OpportunityCreateRequest
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service import LossReason, OpportunityState
from domains.opportunities.service_impl import (
    HandoffPolicy,  # 构造依赖：demo composition root 装配入口
    OpportunityServiceImpl,
    OpportunityUnitOfWork,  # 构造依赖：demo composition root 装配入口
)
from infra.db.session import create_engine_from
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from shared.errors import InvalidStateTransition, PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

_USD = CurrencyCode("USD")


class _DemoAuthorizer:
    """演示用放行 authorizer：只对已知 action 放行（**非生产策略**）。

    所有 service 入口仍会调用 ``require``——本基座只是放行策略，不是旁路。
    """

    def require(
        self,
        actor: Actor,
        action: OpportunityAction,
        scope: OpportunityScope,
        tenant_id: TenantId,
    ) -> str:
        if not isinstance(action, OpportunityAction):
            raise PermissionDenied(f"未知 action: {action}")
        return "demo:allow"


class _NoopAudit:
    """演示用空审计（authorizer Protocol 注入要求；不打印，避免污染演示输出）。"""

    def log(self, **kwargs: object) -> None:
        return None


async def main() -> None:
    url = os.environ["DATABASE_URL"]  # 连接串只来自环境；不打印/记录
    engine = create_engine_from(url)
    try:
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        # 每次运行唯一租户/ID，多跑不冲突；前缀保证总长 <= 32（DB String(32)）。
        tenant_id = TenantId(new_id("tn"))
        employee_id = EmployeeId(new_id("emp"))
        need_id = new_id("need")
        account_id = new_id("acc")

        # 演示用本地策略：显式注入值，**非生产默认策略**（生产由上层装配/Playbook 提供）。
        policy = ScoringPolicy(
            version="demo-gates-v1",
            value_band_boundaries=(
                Money(Decimal(100), _USD),
                Money(Decimal(1000), _USD),
            ),
            bucket_map={
                1: "low",
                2: "low",
                3: "mid",
                4: "mid",
                5: "high",
                6: "high",
                7: "high",
            },
        )
        handoff_policy = HandoffPolicy(sla_seconds=3600, backlog_threshold=40)
        scorer = OpportunityScorerImpl(policy)
        service = OpportunityServiceImpl(
            cast(
                Callable[[], OpportunityUnitOfWork],
                lambda: SqlAlchemyOpportunityUnitOfWork(session_factory, tenant_id),
            ),
            scorer,
            handoff_policy,
            authorizer=_DemoAuthorizer(),  # 演示放行策略（非生产）；require 仍被调用
            audit=_NoopAudit(),
            now=lambda: datetime.now(UTC),  # 注入 UTC aware 时钟，不用 naive 默认
        )
        # 显式 actor：授权身份（manager 作用域）+ 业务确认人 employee_id 分离。
        actor = Actor(
            actor_id=str(employee_id), scope=OpportunityScope(level=ScopeLevel.MANAGER)
        )

        confirmed_at = datetime.now(UTC)  # 人工确认时间（UTC aware）
        request = OpportunityCreateRequest(
            need_id=need_id,
            account_id=account_id,
            account_name="Acme 五金采购部",
            country="US",
            product_category="hinges",
            evidence_tier="high",  # ConfidenceTier.HIGH ≥ 最低档 LOW_MID，过证据门槛
            has_verified_contact=True,
            category_allowed=True,
            minimum_order_value=Money(Decimal(100), _USD),
            estimated_order_value=Money(Decimal(1500), _USD),  # ≥ 底线，过价值门槛
            supply_available=True,
            # 事实来源（EMPLOYEE_INPUT/CONVERSATION），绝不 AGENT_INFERENCE；
            # 覆盖 present 的 account_name/country（硬边界 4/5）。
            field_provenance={
                "account_name": Provenance(
                    source_type=SourceType.EMPLOYEE_INPUT,
                    source_id="demo-emp-1",
                    extracted_by="human",
                    extracted_at=datetime.now(UTC),
                ),
                "country": Provenance(
                    source_type=SourceType.CONVERSATION,
                    source_id="demo-msg-1",
                    extracted_by="human",
                    extracted_at=datetime.now(UTC),
                ),
            },
        )

        # a) 创建机会（真实过门槛）
        opp_id = await service.create_from_need(tenant_id, request, actor=actor)
        if opp_id is None:
            raise RuntimeError("演示失败：机会未通过硬门槛（create_from_need 返回 None）")
        print(f"== 已创建机会 {opp_id}（唯一租户 {tenant_id}） ==")

        # b) 读取真实最新快照：分桶 + 门槛解释
        view = await service.get(tenant_id, opp_id, actor=actor)
        if view.score is None:
            raise RuntimeError("演示失败：get 未返回打分快照（score 为 None）")
        print(f"分桶：{view.score.rank_bucket}")
        print(
            f"门槛解释：passed={view.score.passed_gates} "
            f"failed={view.score.failed_gates} gate_reasons={view.score.gate_reasons}"
        )

        # c) 普通 transition(LOST) 被拒
        try:
            await service.transition(tenant_id, opp_id, OpportunityState.LOST, actor=actor)
        except InvalidStateTransition as exc:
            print(
                f"InvalidStateTransition：{type(exc).__name__} 已捕获"
                "（普通 transition 拒绝终态）"
            )
        else:
            raise RuntimeError("演示失败：普通 transition(LOST) 未抛 InvalidStateTransition")

        # d) mark_lost(reason=None) 被拒
        try:
            await service.mark_lost(
                tenant_id,
                opp_id,
                None,
                actor=actor,
                confirmed_by=employee_id,
                confirmed_at=datetime.now(UTC),
            )
        except MissingLossReasonError as exc:
            print(
                f"MissingLossReasonError：{type(exc).__name__} 已捕获"
                "（终结必须带 LossReason）"
            )
        else:
            raise RuntimeError("演示失败：mark_lost(reason=None) 未抛 MissingLossReasonError")

        # e) 结构化终结（人工确认 + UTC 时间）
        await service.mark_lost(
            tenant_id,
            opp_id,
            LossReason.PRICE_TOO_HIGH,
            actor=actor,
            confirmed_by=employee_id,
            confirmed_at=confirmed_at,
            detail="演示：价格谈不下来",
        )

        # f) 读回实体并验证终态字段（不编造输出）
        async with SqlAlchemyOpportunityUnitOfWork(session_factory, tenant_id) as uow:
            opp = await uow.opportunities.get(tenant_id, opp_id)
            if opp is None:
                raise RuntimeError("演示失败：读回机会为空")
            if opp.state != OpportunityState.LOST:
                raise RuntimeError(f"演示失败：state 应为 lost，实际 {opp.state.value}")
            if opp.died_at_state is None:
                raise RuntimeError("演示失败：died_at_state 为空")
            if opp.loss_reason is None:
                raise RuntimeError("演示失败：loss_reason 为空")
            if opp.closed_by != employee_id:
                raise RuntimeError("演示失败：closed_by 与确认人不一致")
            if opp.closed_at != confirmed_at:
                raise RuntimeError("演示失败：closed_at 与确认时间不一致")
            print(f"died_at_state={opp.died_at_state.value}")
            print(f"loss_reason={opp.loss_reason.value}")
            print(f"closed_by={opp.closed_by}")
        print("== 演示走查完成 ==")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
