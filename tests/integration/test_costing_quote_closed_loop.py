"""T10公开需求晋升→真实工厂成本报价→审批→PDF，不产生发送或成交。"""

import io
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from shared.errors import ValidationError
from shared.schemas.identifiers import new_id


@pytest.mark.parametrize("category,status", [("hardware", "inferred"), ("textiles", "contacting")])
async def test_history_eligibility_projects_persisted_hypothesis_category(category, status):
    case, point, account = history_projection_case(category=category, status=status)
    from tests.integration.costing_quote_case import HistoricalEligibility

    value = await HistoricalEligibility(case).get_contact_eligibility(case.tenant, point, account)
    assert value.qualified_categories == frozenset({category})
    case.demand.get_hypothesis.assert_awaited_once_with(case.tenant, case.hypothesis)


@pytest.mark.parametrize("invalid", ["wrong_account", "rejected", "validated", "missing"])
async def test_history_eligibility_rejects_unqualified_hypothesis(invalid):
    case, point, account = history_projection_case()
    if invalid == "wrong_account":
        case.demand.get_hypothesis.return_value.account_id = new_id("acc")
    elif invalid == "missing":
        case.demand.get_hypothesis.side_effect = ValidationError("受控缺失假设")
    else:
        case.demand.get_hypothesis.return_value.status = invalid
    from tests.integration.costing_quote_case import HistoricalEligibility

    with pytest.raises(ValidationError):
        await HistoricalEligibility(case).get_contact_eligibility(case.tenant, point, account)


def history_projection_case(*, category="hardware", status="inferred"):
    """只测试adapter投影契约；这些替身不用于quote_case真实业务链。"""
    from tests.integration.costing_quote_case import NOW

    tenant, account, point, hypothesis = (new_id(prefix) for prefix in ("tn", "acc", "cp", "hyp"))
    detail = SimpleNamespace(account=SimpleNamespace(country="DE", entity_type="importer"), contacts=[
        SimpleNamespace(contact_points=[SimpleNamespace(
            contact_point=SimpleNamespace(contact_point_id=point,
                verification=SimpleNamespace(value="verified"), verified_at=NOW),
            legal_basis=SimpleNamespace(value="legitimate_interest"), assessment_ref="controlled-history",
        )]),
    ])
    return SimpleNamespace(tenant=tenant, hypothesis=hypothesis,
        demand=SimpleNamespace(get_hypothesis=AsyncMock(return_value=SimpleNamespace(
            account_id=account, category=category, status=status))),
        dependencies=SimpleNamespace(prospecting=SimpleNamespace(get_account_detail=AsyncMock(return_value=detail)))), point, account

def runs_in_isolated_child():
    """Linux宿主不代表已审计环境；只有固定容器入口显式标记子pytest。"""
    return os.environ.get("TRADEOS_T10_ISOLATED_CHILD") == "1"


if runs_in_isolated_child():
    assert sys.platform == "linux", "T10隔离子入口必须运行于Linux"
    from tests.integration.test_need_units import unit_engine

    __all__ = ["unit_engine"]

    @pytest_asyncio.fixture
    async def quote_case(unit_engine, monkeypatch):
        from tests.integration.costing_quote_case import (
            prepare_quote_inputs,
            runtime_case,
        )

        async with runtime_case(unit_engine, monkeypatch) as case:
            await prepare_quote_inputs(case)
            yield case
else:
    @pytest_asyncio.fixture
    async def quote_case():
        """所有宿主只调度已审计Linux容器；不伪造parser或业务fixture。"""
        yield None


async def test_approved_pdf_does_not_send_or_create_a_won_deal(quote_case):
    """PDF交付不得隐式推进发送、成交或需求履约。"""
    if quote_case is None:
        from tests.e2e.costing_quote_lifecycle import run_supervised

        result = await run_supervised("integration")
        code, output = result.code, result.output
        assert code == 0, output
        assert "passed" in output and "skipped" not in output
        print(output)
        return
    q = await quote_case.application.create(
        quote_case.tenant, quote_case.command,
        actor_id=quote_case.actor_id, idempotency_key="controlled-quote-1",
    )
    await quote_case.submit_and_approve(q.content.quote_id)
    f = await quote_case.files.generate(
        quote_case.tenant, q.content.quote_id, actor_id=quote_case.file_actor_id,
    )
    assert f.size_bytes > 0
    assert all(quote_case.gateway_calls.get(k, 0) == 0 for k in (
        "email.send", "contact.enrich", "contact.verify", "web.search",
    ))
    from pypdf import PdfReader
    from sqlalchemy import func, select

    from infra.db.tables import (
        OpportunityRow,
        OutreachMessageAttemptRow,
        QuotationRow,
        ValidatedNeedRow,
    )

    metadata, content = await quote_case.files.download(
        quote_case.tenant, q.content.quote_id, f.file_id, actor_id=quote_case.file_actor_id,
    )
    assert metadata.content_hash == f.content_hash
    pdf = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() for page in pdf.pages)
    assert "200.00" in text and "USD" in text
    assert q.content.lines[0].line_total.amount == 200
    assert "minimum_margin_rate" not in text and quote_case.statement not in text
    async with quote_case.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(QuotationRow).where(
            QuotationRow.tenant_id == quote_case.tenant,
            QuotationRow.quote_id == q.content.quote_id,
        )) == 1
        assert await session.scalar(select(QuotationRow.state).where(
            QuotationRow.tenant_id == quote_case.tenant,
            QuotationRow.quote_id == q.content.quote_id,
        )) == "approved"
        assert await session.scalar(select(OpportunityRow.state).where(
            OpportunityRow.tenant_id == quote_case.tenant,
            OpportunityRow.opportunity_id == quote_case.opportunity,
        )) != "won"
        assert await session.scalar(select(ValidatedNeedRow.status).where(
            ValidatedNeedRow.tenant_id == quote_case.tenant,
            ValidatedNeedRow.need_id == quote_case.need,
        )) != "fulfilled"
        assert await session.scalar(select(func.count()).select_from(OutreachMessageAttemptRow).where(
            OutreachMessageAttemptRow.tenant_id == quote_case.tenant,
            OutreachMessageAttemptRow.state == "sent",
        )) == 1  # 显式模拟历史回执，不是本次Quote发送。


@pytest.mark.parametrize("state", ["no_reply", "replied", "unknown"])
async def test_history_reply_eligibility_uses_current_scoped_fact_reader(state):
    from domains.prospecting.schemas import ContactPointKind
    from shared.errors import TransientError
    from tests.integration.costing_quote_case import NOW, HistoricalEligibility

    tenant, account, point = (new_id(prefix) for prefix in ("tn", "acc", "cp"))
    conversations = SimpleNamespace(get_account_reply_status=AsyncMock(return_value=SimpleNamespace(
        tenant_id=tenant, account_id=account, state=state,
        replied_at=NOW if state == "replied" else None,
    )))
    prospecting = SimpleNamespace(get_outreach_contact_facts=AsyncMock(return_value=SimpleNamespace(
        tenant_id=tenant, account_id=account, contact_point_id=point, kind=ContactPointKind.EMAIL,
    )))
    reader = HistoricalEligibility(SimpleNamespace(
        tenant=tenant, dependencies=SimpleNamespace(conversations=conversations, prospecting=prospecting),
    ))
    if state == "unknown":
        with pytest.raises(TransientError):
            await reader.get_reply_status(tenant, point, account)
    else:
        result = await reader.get_reply_status(tenant, point, account)
        assert result.state.value == state
        assert result.replied_at == (NOW if state == "replied" else None)
    prospecting.get_outreach_contact_facts.assert_awaited_once_with(tenant, account, point)
    conversations.get_account_reply_status.assert_awaited_once_with(tenant, account)


@pytest.mark.parametrize("mismatch", ["tenant", "contact_point"])
async def test_history_reply_eligibility_rejects_mismatched_binding(mismatch):
    from domains.prospecting.schemas import ContactPointKind
    from tests.integration.costing_quote_case import HistoricalEligibility

    tenant, account, point = (new_id(prefix) for prefix in ("tn", "acc", "cp"))
    conversations = SimpleNamespace(get_account_reply_status=AsyncMock(return_value=SimpleNamespace(
        tenant_id=tenant, account_id=account, state="no_reply", replied_at=None,
    )))
    prospecting = SimpleNamespace(get_outreach_contact_facts=AsyncMock(return_value=SimpleNamespace(
        tenant_id=tenant, account_id=account, contact_point_id=new_id("cp"), kind=ContactPointKind.EMAIL,
    )))
    reader = HistoricalEligibility(SimpleNamespace(
        tenant=tenant, dependencies=SimpleNamespace(conversations=conversations, prospecting=prospecting),
    ))
    with pytest.raises(ValidationError):
        await reader.get_reply_status(new_id("tn") if mismatch == "tenant" else tenant, point, account)
    if mismatch == "tenant":
        prospecting.get_outreach_contact_facts.assert_not_awaited()
        conversations.get_account_reply_status.assert_not_awaited()
