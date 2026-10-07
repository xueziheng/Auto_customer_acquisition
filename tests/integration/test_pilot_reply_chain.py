"""当前 pilot 分类器经真实入站、持久工作流与 HTTP 接管的验收。"""

import pytest
from sqlalchemy import text

from agent_runtime.qualification_agent.deterministic import DeterministicReplyClassifier
from shared.schemas.identifiers import EmployeeId
from tests.integration.test_email_inbound_access import composition
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.integration.test_email_inbound_page import NOW, prepare_sent
from tests.integration.test_email_inbound_page import page_runtime as _page_runtime
from tests.integration.test_reply_completion import (
    advance,
    configure_playbook,
    factory_for,
)
from tests.unit.test_email_inbound import mime

page_runtime = _page_runtime


@pytest.mark.parametrize(
    "body,scope",
    [
        ("Please take me off your mailing list.", "contact"),
        ("Please remove me and my assistant from your list.", "account"),
        ("请把我们公司从邮件名单中删除。", "account"),
    ],
)
async def test_pilot_inbound_unsubscribe_persists_once_and_stops_sequence(
    page_runtime, body, scope
):
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        async with factory_for(
            runtime, classifier=DeterministicReplyClassifier()
        )() as worker:
            await runtime["provider"].receive_inbound(
                mime(
                    body=body,
                    headers="Content-Type: text/plain; charset=utf-8",
                    message_id="<pilot-optout@example.test>",
                    reply=runtime["outbound"],
                ),
                internal_date=NOW,
            )
            for _ in range(3):
                await worker.inbound_driver.scan_once()
                await advance(worker, tenant)
        async with runtime["factory"]() as session:
            classifications = (
                await session.execute(
                    text(
                        "SELECT category,suppress_scope FROM conversation_classifications WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).all()
            assert classifications == [("unsubscribe", scope)]
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM outreach_suppressions WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
                == 1
            )
            rows = (
                await session.execute(
                    text(
                        "SELECT account_id,contact_point_id FROM outreach_suppressions WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).all()
            assert len(rows) == 1
            assert (rows[0].contact_point_id is None) == (scope == "account")
            assert (
                await session.scalar(
                    text("SELECT state FROM outreach_enrollments WHERE tenant_id=:t"),
                    {"t": tenant},
                )
                == "replied"
            )
            assert (
                await session.execute(
                    text(
                        "SELECT status,last_error FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                    ),
                    {"t": tenant},
                )
            ).all() == [("completed", None)]
            for table in ("validated_needs", "opportunities", "handoffs"):
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE tenant_id=:t"),
                        {"t": tenant},
                    )
                    == 0
                )
    finally:
        await inbound.aclose()


async def test_rule_mode_cannot_extract_specification(page_runtime):
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    body = "We need hinges for cabinet doors. We need 5000 units at USD 2 per unit."
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        async with factory_for(
            runtime, classifier=DeterministicReplyClassifier()
        )() as worker:
            await configure_playbook(runtime, worker)
            await runtime["provider"].receive_inbound(
                mime(
                    body=body,
                    headers="Content-Type: text/plain; charset=utf-8",
                    message_id="<pilot-need@example.test>",
                    reply=runtime["outbound"],
                ),
                internal_date=NOW,
            )
            await worker.inbound_driver.scan_once()
            await worker.inbound_driver.scan_once()
            await advance(worker, tenant, 8)
            async with runtime["factory"]() as session:
                runs = (
                    await session.execute(
                        text(
                            "SELECT status,last_error FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                        ),
                        {"t": tenant},
                    )
                ).all()
                assert runs == [("failed", "step classify failed: ValidationError")]
                for table in (
                    "conversation_classifications",
                    "validated_needs",
                    "opportunities",
                    "handoffs",
                ):
                    assert (
                        await session.scalar(
                            text(f"SELECT count(*) FROM {table} WHERE tenant_id=:t"),
                            {"t": tenant},
                        )
                        == 0
                    )
    finally:
        await inbound.aclose()
