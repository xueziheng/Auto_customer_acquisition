"""私有测试正向回复只进入原owner工作项，不形成正式业务成果。"""
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.composition_support.employee_readers import employee_service_scope
from apps.scheduler_worker.reply_binding import ReplyRuntimeResources
from apps.scheduler_worker.reply_composition import CurrentEmployeeReplyActions
from domains.conversations.schemas import ReplyCategory, ReplyWorkStatus
from domains.employees.permissions import Phase1EmployeeAuthorizer, StandardAuditLogger
from infra.db.session import create_engine_from
from infra.db.tables import (
    HandoffRow,
    NeedHypothesisRow,
    OpportunityRow,
    OutreachMessageAttemptRow,
    ValidatedNeedRow,
)
from shared.schemas.identifiers import new_id
from tests.integration.test_conversation_reply_work_actions import _service
from tests.integration.test_manual_email_send_api import _NOW, _seed_employee
from tests.integration.test_qq_inbound_scope import seed
from workflows.reply_qualification.ports import ReplyActionContext


@pytest.mark.parametrize("category,methods,positive", [
    ("clear_interest", ("start_qualification",), True),
    ("willing_to_continue", ("start_qualification",), True),
    ("requests_quote", ("request_handoff",), True),
    ("provides_specification", ("extract_need_fields", "request_handoff"), True),
    ("no_current_need", ("create_follow_up",), False),
    ("rejection", ("create_follow_up",), False),
])
async def test_private_reply_actions_do_not_promote_business(db_url, category, methods, positive):
    engine = create_engine_from(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant, boss = new_id("tn"), new_id("emp")
    try:
        _route, _owner, _user, header, attempt, account, point = await seed(sessions, tenant)
        await _seed_employee(sessions, tenant, boss)
        conversations = _service(sessions, tenant)
        message = await conversations.ingest_inbound(
            tenant, None, account, new_id("art"), "<private-fixture@qq.com>", _NOW, outbound_message_id=header)
        await conversations.record_classification(tenant, message, ReplyCategory(category), "fixture:v1", outbound_message_id=header)
        async with sessions() as db:
            enrollment = await db.scalar(select(OutreachMessageAttemptRow.enrollment_id).where(
                OutreachMessageAttemptRow.tenant_id == tenant, OutreachMessageAttemptRow.attempt_id == attempt))
        scope = partial(employee_service_scope, sessions, now=lambda: _NOW,
            authorizer=Phase1EmployeeAuthorizer(tenant), audit=StandardAuditLogger())
        core = SimpleNamespace(employee_scope=scope, conversations=conversations)
        # 非测试分支访问其他业务端口会直接失败，禁止用fake成功掩盖业务写入。
        actions = CurrentEmployeeReplyActions(tenant, boss, core, None,
            ReplyRuntimeResources(sessions, None, None, lambda: _NOW), None, None)
        context = ReplyActionContext(message, header, enrollment, account, point)
        for method in (*methods, *methods):
            await getattr(actions, method)(tenant, context, "fixture:" + method)
        queue = await conversations.list_reply_work_queue(tenant, status=ReplyWorkStatus.PENDING, limit=10)
        assert len(queue) == int(positive)
        if queue:
            assert queue[0].account_id == account and queue[0].message_id == message
        async with sessions() as db:
            for row in (NeedHypothesisRow, ValidatedNeedRow, OpportunityRow, HandoffRow):
                assert await db.scalar(select(func.count()).select_from(row).where(row.tenant_id == tenant)) == 0
    finally:
        await engine.dispose()
