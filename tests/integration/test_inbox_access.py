"""当前归属真实PG权限；复用owned基础设施，不seed审批或需求终态。"""

from dataclasses import replace
from datetime import timedelta

import pytest

# ruff: noqa: F811 - 复用owned生命周期fixture
from domains.conversations.schemas import ReplyCategory
from domains.conversations.service import InboxActor, InboxScope
from domains.employees.permissions import Actor, EmployeeScope
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId, new_id
from tests.integration.test_email_inbound_access import composition
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.integration.test_email_inbound_page import NOW, page_runtime  # noqa: F401
from tests.runtime_database_fixtures import runtime_database_url  # noqa: F401


def inbox_actor(runtime, role):
    employee = next(i for i in runtime["staff"] if i.role == role)
    owners = frozenset(
        EmployeeId(i.employee_id)
        for i in runtime["staff"]
        if (i.role == "sales" and role == "manager") or i == employee
    )
    return InboxActor(
        runtime["route"].tenant_id,
        EmployeeId(employee.employee_id),
        role,
        {
            "boss": InboxScope.TENANT,
            "manager": InboxScope.MANAGER,
            "sales": InboxScope.SELF,
        }.get(role, InboxScope.SELF),
        owners,
    )


async def message_for(runtime, owner=None, *, stamp=NOW):
    tenant = runtime["route"].tenant_id
    account = ProspectAccountId(new_id("acc"))
    service = runtime["deps"].conversations
    message = await service.ingest_inbound(
        tenant, None, account, new_id("raw"), f"<{new_id('ext')}@example.test>", stamp
    )
    if owner is not None:
        boss = inbox_actor(runtime, "boss")
        async with runtime["deps"].employees(tenant) as employees:
            await employees.resolve_owner(
                tenant,
                account,
                actor=Actor(str(boss.employee_id), EmployeeScope.TENANT, "boss"),
                country="US",
                boss_override=owner,
            )
    ref = await service.get_message_evidence(
        tenant, message, actor=inbox_actor(runtime, "boss")
    )
    return account, message, ref.conversation_id


@pytest.mark.parametrize("role", ["boss", "manager", "sales", "product"])
@pytest.mark.parametrize("ownership", ["self", "sales", "boss", "missing"])
async def test_all_actions_current_owner_matrix(page_runtime, role, ownership):
    runtime = page_runtime
    c = await composition(runtime)
    try:
        actor = inbox_actor(runtime, role)
        owner = (
            None
            if ownership == "missing"
            else inbox_actor(
                runtime, role if ownership == "self" else ownership
            ).employee_id
        )
        account, message, conversation = await message_for(runtime, owner)
        allowed = (
            role == "boss"
            or role in {"sales", "manager"}
            and (
                owner == actor.employee_id or role == "manager" and ownership == "sales"
            )
        )
        service, tenant = runtime["deps"].conversations, runtime["route"].tenant_id
        if role in {"boss", "manager", "sales"}:
            listed = await service.list_inbox(
                tenant, actor=actor, category=None, limit=1
            )
            assert bool(listed) == allowed
        else:
            with pytest.raises(PermissionDenied):
                await service.list_inbox(tenant, actor=actor, category=None, limit=1)
        from domains.conversations.service import InboxAction

        for action in (
            InboxAction.READ,
            InboxAction.EVIDENCE_READ,
            InboxAction.NEXT_QUESTIONS,
        ):
            if allowed:
                assert (
                    await service.get_message_evidence(
                        tenant, message, actor=actor, action=action
                    )
                ).account_id == account
            else:
                with pytest.raises(PermissionDenied):
                    await service.get_message_evidence(
                        tenant, message, actor=actor, action=action
                    )
        if allowed:
            assert (
                await service.get_inbox_detail(tenant, conversation, actor=actor)
            ).account_id == account
        else:
            with pytest.raises(PermissionDenied):
                await service.get_inbox_detail(tenant, conversation, actor=actor)
        # 分类来自原公开record方法；没有审批/验证结果插入。
        await service.record_classification(
            tenant, message, ReplyCategory.REJECTION, "controlled:v1"
        )
        if allowed:
            await service.correct_classification(
                tenant,
                message,
                ReplyCategory.REQUESTS_QUOTE,
                actor.employee_id,
                actor=actor,
            )
        else:
            with pytest.raises(PermissionDenied):
                await service.correct_classification(
                    tenant,
                    message,
                    ReplyCategory.REQUESTS_QUOTE,
                    actor.employee_id,
                    actor=actor,
                )
    finally:
        await c.aclose()


async def test_foreign_recent_window_does_not_hide_authorized_conversation(
    page_runtime,
):
    runtime = page_runtime
    c = await composition(runtime)
    try:
        sales, boss = inbox_actor(runtime, "sales"), inbox_actor(runtime, "boss")
        account, _, _ = await message_for(runtime, sales.employee_id)
        for index in range(201):
            await message_for(
                runtime, boss.employee_id, stamp=NOW + timedelta(seconds=index + 1)
            )
        rows = await runtime["deps"].conversations.list_inbox(
            sales.tenant_id, actor=sales, category=None, limit=1
        )
        assert len(rows) == 1 and rows[0].account_id == account
    finally:
        await c.aclose()


async def test_forged_scope_and_cross_tenant_fail_closed(page_runtime):
    runtime = page_runtime
    c = await composition(runtime)
    try:
        sales, boss = inbox_actor(runtime, "sales"), inbox_actor(runtime, "boss")
        _, message, conversation = await message_for(runtime, boss.employee_id)
        for actor in (
            replace(sales, allowed_owner_ids=frozenset({boss.employee_id})),
            replace(sales, role="boss", scope=InboxScope.TENANT),
            replace(boss, tenant_id=TenantId(new_id("tn"))),
        ):
            with pytest.raises(PermissionDenied):
                await runtime["deps"].conversations.get_inbox_detail(
                    sales.tenant_id, conversation, actor=actor
                )
        with pytest.raises(PermissionDenied):
            await runtime["deps"].conversations.correct_classification(
                sales.tenant_id,
                message,
                ReplyCategory.REJECTION,
                boss.employee_id,
                actor=sales,
            )
    finally:
        await c.aclose()


async def test_message_evidence_gateway_requires_current_owner(page_runtime):
    from apps.api.inbox_evidence import build_inbox_evidence_reader
    from infra.email_inbound_artifacts import InboundRawArtifactArchiver
    from tests.unit.test_email_inbound import mime
    from tool_gateway.errors import ToolGatewayError

    runtime = page_runtime
    c = await composition(runtime)
    try:
        sales, boss = inbox_actor(runtime, "sales"), inbox_actor(runtime, "boss")
        raw_bytes = mime(body="<script>never execute</script>")
        # 原archiver公开方法和真实MinIO，而非seed artifact row。
        raw = await InboundRawArtifactArchiver(
            c.bounded_raw_store, c.bounded_raw_store
        ).archive(sales.tenant_id, raw_bytes, maximum_bytes=2 * 1024 * 1024)
        account, _, _ = await message_for(runtime, sales.employee_id)
        message = await runtime["deps"].conversations.ingest_inbound(
            sales.tenant_id,
            None,
            account,
            raw.artifact_id,
            f"<{new_id('ext')}@example.test>",
            NOW,
        )
        reader = build_inbox_evidence_reader(
            sales.tenant_id,
            runtime["factory"],
            runtime["deps"].conversations,
            c.bounded_raw_store,
            now=lambda: NOW,
        )
        assert await reader.read(sales.tenant_id, message, actor=sales) == raw_bytes
        async with runtime["deps"].employees(sales.tenant_id) as employees:
            await employees.transfer(
                sales.tenant_id,
                account,
                boss.employee_id,
                actor=Actor(str(boss.employee_id), EmployeeScope.TENANT, "boss"),
                transferred_by=boss.employee_id,
                reason="测试真实转移",
            )
        with pytest.raises((PermissionDenied, ToolGatewayError)):
            await reader.read(sales.tenant_id, message, actor=sales)
        assert await reader.read(sales.tenant_id, message, actor=boss) == raw_bytes
    finally:
        await c.aclose()


async def change_access(runtime, account, change):
    """既有EmployeeRepository.update/EmployeeService.transfer真实事务，不插终态。"""
    from infra.db.repositories.employees import EmployeeRepositoryImpl

    sales, boss = inbox_actor(runtime, "sales"), inbox_actor(runtime, "boss")
    if change == "transfer":
        async with runtime["deps"].employees(sales.tenant_id) as employees:
            await employees.transfer(
                sales.tenant_id,
                account,
                boss.employee_id,
                actor=Actor(str(boss.employee_id), EmployeeScope.TENANT, "boss"),
                transferred_by=boss.employee_id,
                reason="测试当前归属转移",
            )
    else:
        async with runtime["factory"].begin() as session:
            repository = EmployeeRepositoryImpl(session, sales.tenant_id)
            current = await repository.get(sales.tenant_id, sales.employee_id)
            changed = replace(
                current,
                **(
                    {"is_active": False}
                    if change == "deactivate"
                    else {"manager_id": boss.employee_id}
                ),
            )
            await repository.update(changed)


@pytest.mark.parametrize("change", ["transfer", "deactivate", "manager"])
async def test_correction_lock_linearizes_real_current_fact_change(
    page_runtime, monkeypatch, change
):
    import asyncio

    from sqlalchemy import text

    from infra.db.repositories.conversations import ClassificationRepositoryImpl

    runtime = page_runtime
    c = await composition(runtime)
    release, inside = asyncio.Event(), asyncio.Event()
    tasks = []
    try:
        sales = inbox_actor(runtime, "sales")
        actor = inbox_actor(runtime, "manager") if change == "manager" else sales
        account, message, conversation = await message_for(runtime, sales.employee_id)
        service = runtime["deps"].conversations
        await service.record_classification(
            actor.tenant_id, message, ReplyCategory.REJECTION, "controlled:v1"
        )
        original = ClassificationRepositoryImpl.add_correction
        correction_pid: int | None = None

        async def hold(self, correction):
            nonlocal correction_pid
            correction_pid = await self._session.scalar(text("SELECT pg_backend_pid()"))
            inside.set()
            await release.wait()
            return await original(self, correction)

        monkeypatch.setattr(ClassificationRepositoryImpl, "add_correction", hold)
        correction = asyncio.create_task(
            service.correct_classification(
                actor.tenant_id,
                message,
                ReplyCategory.REQUESTS_QUOTE,
                actor.employee_id,
                actor=actor,
            )
        )
        tasks.append(correction)
        await asyncio.wait_for(inside.wait(), 3)
        assert isinstance(correction_pid, int)
        change_task = asyncio.create_task(change_access(runtime, account, change))
        tasks.append(change_task)
        # 第三条连接查看真实PG阻塞，不能用单连接savepoint或睡眠冒充竞争。
        blocked = False
        async with runtime["factory"]() as session:
            for _ in range(100):
                # 同一事务会缓存活动快照；每轮刷新才能看见稍后建立的变更连接。
                await session.execute(text("SELECT pg_stat_clear_snapshot()"))
                blocked = bool(
                    (
                        await session.execute(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND :correction_pid = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"correction_pid": correction_pid},
                        )
                    ).scalar_one()
                )
                if blocked or change_task.done():
                    break
                await asyncio.sleep(0.01)
        if change_task.done():
            await change_task  # 提前失败必须暴露原异常，不能伪装成未观察到锁。
        assert blocked and not change_task.done(), (
            "current fact UPDATE must block until correction commit"
        )
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        # 新事实先提交后，持旧snapshot/旧链接的所有入口都拒绝。
        with pytest.raises(PermissionDenied):
            await service.get_inbox_detail(actor.tenant_id, conversation, actor=actor)
        with pytest.raises(PermissionDenied):
            await service.correct_classification(
                actor.tenant_id,
                message,
                ReplyCategory.CLEAR_INTEREST,
                actor.employee_id,
                actor=actor,
            )
        with pytest.raises(PermissionDenied):
            await service.get_message_evidence(actor.tenant_id, message, actor=actor)
        boss = inbox_actor(runtime, "boss")
        detail = await service.get_inbox_detail(
            boss.tenant_id, conversation, actor=boss
        )
        assert detail.messages[0].original_category is ReplyCategory.REJECTION
        assert len(detail.messages[0].corrections) == 1
    finally:
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await c.aclose()


@pytest.mark.parametrize("mode", ["transfer_during_read", "wrong_kind", "other_tenant"])
async def test_raw_gateway_rechecks_and_rejects_mismatched_artifact(
    page_runtime, runtime_database_url, monkeypatch, mode
):
    from apps.api.inbox_evidence import build_inbox_evidence_reader
    from artifact_store.store import RawArtifactKind
    from tests.unit.test_email_inbound import mime
    from tool_gateway.errors import ToolGatewayError

    runtime = page_runtime
    c = await composition(runtime)
    try:
        sales = inbox_actor(runtime, "sales")
        tenant = sales.tenant_id
        raw_bytes = mime()
        if mode == "other_tenant":
            from contextlib import AsyncExitStack

            from sqlalchemy.ext.asyncio import async_sessionmaker

            from artifact_store.service_impl import RawArtifactStoreImpl
            from connectors.object_store.config import S3ObjectStoreSettings
            from connectors.object_store.s3 import S3ObjectBlobTransport
            from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
            from infra.db.session import create_engine_from

            other_tenant = TenantId(new_id("tn"))
            other_url = await runtime_database_url(other_tenant)
            async with AsyncExitStack() as resources:
                engine = create_engine_from(other_url)
                resources.push_async_callback(engine.dispose)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                settings = S3ObjectStoreSettings.from_environ(
                    runtime["config"].runtime_environment(),
                )
                transport = S3ObjectBlobTransport(settings, runtime["config"])
                resources.push_async_callback(transport.aclose)
                foreign_store = RawArtifactStoreImpl(
                    lambda tenant_id: SqlAlchemyArtifactUnitOfWork(sessions, tenant_id),
                    transport, settings.raw_max_bytes, lambda: NOW, new_id,
                )
                meta = await foreign_store.put(
                    other_tenant, RawArtifactKind.EMAIL_RAW, raw_bytes,
                    "message/rfc822", uploaded_by=None,
                )
        else:
            meta = await c.bounded_raw_store.put(
                tenant,
                RawArtifactKind.EMAIL_RAW
                if mode != "wrong_kind"
                else RawArtifactKind.WEB_SNAPSHOT,
                raw_bytes,
                "text/html" if mode == "wrong_kind" else "message/rfc822",
                uploaded_by=None,
            )
        account, _, _ = await message_for(runtime, sales.employee_id)
        message = await runtime["deps"].conversations.ingest_inbound(
            tenant,
            None,
            account,
            str(meta.artifact_id),
            f"<{new_id('ext')}@example.test>",
            NOW,
        )
        reader = build_inbox_evidence_reader(
            tenant,
            runtime["factory"],
            runtime["deps"].conversations,
            c.bounded_raw_store,
            now=lambda: NOW,
        )
        original = c.bounded_raw_store.get_bounded

        async def read_and_transfer(*args, **kwargs):
            result = await original(*args, **kwargs)
            await change_access(runtime, account, "transfer")
            return result

        if mode == "transfer_during_read":
            monkeypatch.setattr(c.bounded_raw_store, "get_bounded", read_and_transfer)
        with pytest.raises((PermissionDenied, ToolGatewayError)):
            await reader.read(tenant, message, actor=sales)
    finally:
        await c.aclose()


async def test_http_message_evidence_download_headers_and_no_arbitrary_ref(
    page_runtime,
):
    from httpx import ASGITransport, AsyncClient

    from apps.api.inbox_evidence import build_inbox_evidence_reader
    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings
    from artifact_store.store import RawArtifactKind
    from tests.unit.test_email_inbound import mime

    runtime = page_runtime
    c = await composition(runtime)
    try:
        sales = inbox_actor(runtime, "sales")
        tenant = sales.tenant_id
        raw_bytes = mime(body="<script>never execute</script>")
        meta = await c.bounded_raw_store.put(
            tenant,
            RawArtifactKind.EMAIL_RAW,
            raw_bytes,
            "message/rfc822",
            uploaded_by=None,
        )
        account, _, _ = await message_for(runtime, sales.employee_id)
        message = await runtime["deps"].conversations.ingest_inbound(
            tenant,
            None,
            account,
            str(meta.artifact_id),
            f"<{new_id('ext')}@example.test>",
            NOW,
        )
        deps = replace(
            runtime["deps"],
            inbox_evidence=build_inbox_evidence_reader(
                tenant,
                runtime["factory"],
                runtime["deps"].conversations,
                c.bounded_raw_store,
                now=lambda: NOW,
            ),
        )
        app = create_app(
            settings=ApiSettings(
                tenant_id=tenant, dev_mode=True, retry_after_seconds=2
            ),
            dependencies=deps,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://controlled.test",
            headers={"X-Employee-Id": sales.employee_id, "X-Tenant-Id": tenant},
        ) as client:
            path = f"/inbox/messages/{message}/evidence"
            result = await client.get(path)
            assert result.status_code == 200 and result.content == raw_bytes
            assert result.headers["cache-control"] == "private, no-store"
            assert result.headers["x-content-type-options"] == "nosniff"
            assert result.headers["content-type"] == "application/octet-stream"
            assert (
                result.headers["content-disposition"]
                == f'attachment; filename="{message}.eml"'
            )
            assert (
                await client.get(path, params={"artifact_id": str(meta.artifact_id)})
            ).status_code == 400
            assert (await client.request("GET", path, content=b"{}")).status_code == 400
            await change_access(runtime, account, "transfer")
            assert (await client.get(path)).status_code == 403
    finally:
        await c.aclose()


@pytest.mark.parametrize(
    "role", ["viewer", "product", "sourcing", "finance", "unknown", "system"]
)
async def test_other_roles_cannot_use_any_inbox_action(page_runtime, role):
    from domains.conversations.service import InboxAction

    runtime = page_runtime
    c = await composition(runtime)
    try:
        boss = inbox_actor(runtime, "boss")
        _, message, _ = await message_for(runtime, boss.employee_id)
        actor = replace(boss, role=role)
        for action in InboxAction:
            with pytest.raises(PermissionDenied):
                await runtime["deps"].conversations.get_message_evidence(
                    boss.tenant_id, message, actor=actor, action=action
                )
    finally:
        await c.aclose()


@pytest.mark.parametrize("change", ["transfer", "deactivate", "manager"])
async def test_current_fact_transaction_wins_before_correction(
    page_runtime, monkeypatch, change
):
    import asyncio

    from sqlalchemy import text

    from infra.db.repositories.employees import (
        EmployeeRepositoryImpl,
        OwnershipRepositoryImpl,
    )

    runtime = page_runtime
    c = await composition(runtime)
    inside, release = asyncio.Event(), asyncio.Event()
    tasks = []
    try:
        sales = inbox_actor(runtime, "sales")
        actor = inbox_actor(runtime, "manager") if change == "manager" else sales
        account, message, conversation = await message_for(runtime, sales.employee_id)
        service = runtime["deps"].conversations
        await service.record_classification(
            actor.tenant_id, message, ReplyCategory.REJECTION, "controlled:v1"
        )
        cls, method = (
            (OwnershipRepositoryImpl, "replace")
            if change == "transfer"
            else (EmployeeRepositoryImpl, "update")
        )
        original = getattr(cls, method)
        update_pid: int | None = None

        async def hold_update(self, *args, **kwargs):
            nonlocal update_pid
            result = await original(self, *args, **kwargs)
            update_pid = await self._session.scalar(text("SELECT pg_backend_pid()"))
            inside.set()
            await release.wait()
            return result

        monkeypatch.setattr(cls, method, hold_update)
        update_task = asyncio.create_task(change_access(runtime, account, change))
        tasks.append(update_task)
        await asyncio.wait_for(inside.wait(), 3)
        assert isinstance(update_pid, int)
        correction = asyncio.create_task(
            service.correct_classification(
                actor.tenant_id,
                message,
                ReplyCategory.REQUESTS_QUOTE,
                actor.employee_id,
                actor=actor,
            )
        )
        tasks.append(correction)
        blocked = False
        async with runtime["factory"]() as session:
            for _ in range(100):
                await session.execute(text("SELECT pg_stat_clear_snapshot()"))
                blocked = bool(
                    (
                        await session.execute(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND :update_pid = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"update_pid": update_pid},
                        )
                    ).scalar_one()
                )
                if blocked or correction.done():
                    break
                await asyncio.sleep(0.01)
        if correction.done():
            await correction
        assert blocked and not correction.done()
        release.set()
        await asyncio.wait_for(update_task, 3)
        with pytest.raises(PermissionDenied):
            await asyncio.wait_for(correction, 3)
        detail = await service.get_inbox_detail(
            actor.tenant_id, conversation, actor=inbox_actor(runtime, "boss")
        )
        assert detail.messages[0].corrections == ()
    finally:
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await c.aclose()


async def test_next_questions_unavailable_branch_rechecks_after_transfer(
    page_runtime, monkeypatch
):
    runtime = page_runtime
    c = await composition(runtime)
    try:
        actor = inbox_actor(runtime, "sales")
        account, message, conversation = await message_for(runtime, actor.employee_id)
        service = runtime["deps"].conversations
        original = service.get_inbox_detail

        async def detail_then_transfer(*args, **kwargs):
            detail = await original(*args, **kwargs)
            await change_access(runtime, account, "transfer")
            return detail

        monkeypatch.setattr(service, "get_inbox_detail", detail_then_transfer)
        with pytest.raises(PermissionDenied):
            await runtime["deps"].reply_suggestions.read(
                actor.tenant_id, actor.employee_id, conversation, message, actor=actor
            )
    finally:
        await c.aclose()


async def test_manager_snapshot_cannot_gain_new_direct_or_grandchild_scope(
    page_runtime,
):
    from infra.db.repositories.employees import EmployeeRepositoryImpl

    runtime = page_runtime
    c = await composition(runtime)
    try:
        manager, sales, boss = (
            inbox_actor(runtime, role) for role in ("manager", "sales", "boss")
        )
        account, message, _ = await message_for(runtime, sales.employee_id)
        service = runtime["deps"].conversations
        # 新直属不在请求snapshot集合时不能在本请求内新增权限。
        narrowed = replace(manager, allowed_owner_ids=frozenset({manager.employee_id}))
        with pytest.raises(PermissionDenied):
            await service.get_message_evidence(
                manager.tenant_id, message, actor=narrowed
            )
        async with runtime["factory"].begin() as session:
            repo = EmployeeRepositoryImpl(session, manager.tenant_id)
            employee = await repo.get(manager.tenant_id, sales.employee_id)
            await repo.update(replace(employee, is_active=False))
        with pytest.raises(PermissionDenied):
            await service.get_message_evidence(
                manager.tenant_id, message, actor=manager
            )
        assert (
            await service.get_message_evidence(boss.tenant_id, message, actor=boss)
        ).account_id == account
        # 直属员工变成孙级，即使旧snapshot仍包含其ID，也不可见。
        intermediate = inbox_actor(runtime, "product").employee_id
        async with runtime["factory"].begin() as session:
            repo = EmployeeRepositoryImpl(session, manager.tenant_id)
            employee = await repo.get(manager.tenant_id, sales.employee_id)
            parent = await repo.get(manager.tenant_id, intermediate)
            await repo.update(replace(parent, manager_id=manager.employee_id))
            await repo.update(
                replace(employee, is_active=True, manager_id=intermediate)
            )
        with pytest.raises(PermissionDenied):
            await service.get_message_evidence(
                manager.tenant_id, message, actor=manager
            )
        assert (
            await service.list_inbox(
                manager.tenant_id, actor=manager, category=None, limit=20
            )
            == []
        )
    finally:
        await c.aclose()


async def test_request_role_snapshot_does_not_survive_promotion_or_demotion(
    page_runtime,
):
    from infra.db.repositories.employees import EmployeeRepositoryImpl

    runtime = page_runtime
    c = await composition(runtime)
    try:
        sales, boss = inbox_actor(runtime, "sales"), inbox_actor(runtime, "boss")
        _, message, _ = await message_for(runtime, sales.employee_id)
        async with runtime["factory"].begin() as session:
            repo = EmployeeRepositoryImpl(session, sales.tenant_id)
            employee = await repo.get(sales.tenant_id, sales.employee_id)
            await repo.update(replace(employee, role=type(employee.role)("boss")))
            principal = await repo.get(boss.tenant_id, boss.employee_id)
            await repo.update(replace(principal, role=type(principal.role)("sales")))
        for actor in (sales, boss):
            with pytest.raises(PermissionDenied):
                await runtime["deps"].conversations.get_message_evidence(
                    actor.tenant_id, message, actor=actor
                )
    finally:
        await c.aclose()


async def test_evidence_cannot_combine_old_principal_with_new_ownership(
    page_runtime, monkeypatch
):
    """两次READ COMMITTED事实读取不能拼成任一真实时刻均不存在的权限。"""
    from infra.db.inbox_access import SqlAlchemyInboxAccessFactsReader
    from infra.db.repositories.employees import EmployeeRepositoryImpl

    runtime = page_runtime
    c = await composition(runtime)
    try:
        manager, sales, boss = (
            inbox_actor(runtime, role) for role in ("manager", "sales", "boss")
        )
        account, message, _ = await message_for(runtime, boss.employee_id)
        original = SqlAlchemyInboxAccessFactsReader.read_employee
        changed = False

        async def read_and_change(self, tenant, employee_id):
            nonlocal changed
            current = await original(self, tenant, employee_id)
            if employee_id == manager.employee_id and not changed:
                changed = True
                async with runtime["factory"].begin() as session:
                    repo = EmployeeRepositoryImpl(session, tenant)
                    principal = await repo.get(tenant, manager.employee_id)
                    await repo.update(replace(principal, is_active=False))
                async with runtime["deps"].employees(tenant) as employees:
                    await employees.transfer(
                        tenant,
                        account,
                        sales.employee_id,
                        actor=Actor(
                            str(boss.employee_id), EmployeeScope.TENANT, "boss"
                        ),
                        transferred_by=boss.employee_id,
                        reason="测试授权事实单一快照",
                    )
            return current

        monkeypatch.setattr(
            SqlAlchemyInboxAccessFactsReader, "read_employee", read_and_change
        )
        with pytest.raises(PermissionDenied):
            await runtime["deps"].conversations.get_message_evidence(
                manager.tenant_id, message, actor=manager
            )
    finally:
        await c.aclose()
