"""客户假设选项先精确过滤账户，验证缺证据与非活跃状态不入选。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service_impl import DemandServiceImpl
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.tables import NeedHypothesisRow
from shared.schemas.identifiers import ProspectAccountId, TenantId, new_id

NOW = datetime(2026, 10, 14, tzinfo=UTC)


class Names:
    async def countries_for(self, tenant_id, account_ids):
        return {a: "US" for a in account_ids}

    async def domains_for(self, tenant_id, account_ids):
        return {a: "example.com" for a in account_ids}

    async def names_for(self, tenant_id, account_ids):
        return {a: "测试客户" for a in account_ids}


async def test_exact_account_hypotheses_survive_company_limit_and_rejection(
    integration_engine,
):
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant, account = TenantId(new_id("tn")), ProspectAccountId(new_id("acc"))
    service = DemandServiceImpl(
        lambda t: SqlAlchemyDemandUnitOfWork(factory, t),
        now=lambda: NOW,
        account_names=Names(),
    )
    signal = await service.capture_signal(
        tenant,
        SignalCaptureRequest(
            "product_line_expansion",
            "测试客户",
            "新工厂",
            NOW,
            "employee_input",
            "manual-1",
            "human",
        ),
    )
    hypothesis = await service.create_hypothesis(
        tenant, account, "hinges", [str(signal)], "可能采购五金", "model-test"
    )
    async with factory() as session, session.begin():
        row = (
            await session.scalars(
                select(NeedHypothesisRow).where(
                    NeedHypothesisRow.tenant_id == tenant,
                    NeedHypothesisRow.hypothesis_id == hypothesis,
                )
            )
        ).one()
        values = {
            c.name: getattr(row, c.name) for c in NeedHypothesisRow.__table__.columns
        }
        for _ in range(205):
            session.add(
                NeedHypothesisRow(
                    **{
                        **values,
                        "hypothesis_id": new_id("hyp"),
                        "account_id": new_id("acc"),
                    }
                )
            )
    views = await service.list_account_outreach_hypotheses(tenant, account)
    assert [v.hypothesis_id for v in views] == [hypothesis] and views[0].evidence
    assert (
        await service.list_account_outreach_hypotheses(TenantId(new_id("tn")), account)
        == []
    )
    await service.reject_hypothesis(tenant, hypothesis, "no_need")
    assert await service.list_account_outreach_hypotheses(tenant, account) == []
