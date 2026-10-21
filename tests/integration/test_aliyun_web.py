"""阿里邮箱连接经过真实Gateway账本，凭证不进入账本或状态。"""
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.conversations.aliyun_connection import AliyunActor, AliyunConnectCommand
from infra.db.tables import EmployeeRow, ToolCallRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import new_id
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider


async def setup_service(integration_engine, tmp_path, rejected=False, provider="aliyun"):
    from apps.api.composition.aliyun_web import AliyunWebService

    tmp_path.chmod(0o700)
    actor = AliyunActor(tenant_id=new_id("tn"), employee_id=new_id("emp"), user_id=new_id("usr"))
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    async with sessions.begin() as s:
        s.add(EmployeeRow(tenant_id=actor.tenant_id, employee_id=actor.employee_id,
            user_id=actor.user_id, name="合成员工", role="sales", is_active=True))
    calls = []
    class Authentication:
        def __init__(self, address, password):
            self.address, self.password = address, password
        async def authenticate(self):
            calls.append(self.address)
            assert self.password.get_secret_value() == "synthetic-aliyun-only"
            if rejected:
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
    service_type = AliyunWebService
    if provider == "qq":
        from apps.api.composition.qq_web import QQWebService
        service_type = QQWebService
    service = service_type(tmp_path, sessions, HmacFingerprintProvider("v1", b"t" * 32),
        connector_factory=Authentication)
    command = AliyunConnectCommand(email="sales@example.com", password="synthetic-aliyun-only", confirm_connect=True)
    return service, actor, command, calls


async def test_qq_authentication_uses_its_own_gateway_plugin(integration_engine, tmp_path):
    service, actor, command, calls = await setup_service(integration_engine, tmp_path, provider="qq")
    await service.connect(actor, command)
    assert len(calls) == 1
    async with service.sessions() as s:
        row = await s.scalar(select(ToolCallRow).where(ToolCallRow.tenant_id == actor.tenant_id))
        assert row.tool_id == "email.qq.connect" and row.status == "succeeded"


async def test_connect_via_gateway_then_current_employee_only(integration_engine, tmp_path):
    service, actor, command, calls = await setup_service(integration_engine, tmp_path)
    result = await service.connect(actor, command)
    assert result.email == "sales@example.com" and result.smtp_authenticated and result.imap_authenticated
    assert not result.automatic_reply_enabled
    assert calls == ["sales@example.com"]
    async with service.sessions() as s:
        rows = (await s.scalars(select(ToolCallRow).where(ToolCallRow.tenant_id == actor.tenant_id))).all()
        assert len(rows) == 1 and rows[0].status == "succeeded"
        serialized = repr({c.name: getattr(rows[0], c.name) for c in ToolCallRow.__table__.columns})
        assert "synthetic-aliyun-only" not in serialized and "sales@example.com" not in serialized
    with pytest.raises(PermissionDenied):
        await service.status(actor.model_copy(update={"user_id": new_id("usr")}))
    async with service.sessions.begin() as s:
        await s.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == actor.tenant_id,
            EmployeeRow.employee_id == actor.employee_id).values(is_active=False))
    with pytest.raises(PermissionDenied):
        await service.status(actor)


async def test_failed_authentication_never_publishes_binding(integration_engine, tmp_path):
    service, actor, command, calls = await setup_service(integration_engine, tmp_path, True)
    with pytest.raises(ValidationError):
        await service.connect(actor, command)
    assert calls == ["sales@example.com"]
    assert service.store.binding(actor) is None


async def test_delivery_resolver_requires_current_employee_and_exact_bound_sender(integration_engine, tmp_path):
    from apps.api.composition.aliyun_web import AliyunOutboundConnectors
    service, actor, command, _calls = await setup_service(integration_engine, tmp_path)
    await service.connect(actor, command)
    resolved = []
    def connector(address, password):
        resolved.append(address)
        assert password.get_secret_value() == "synthetic-aliyun-only"
        return object()
    delivery = AliyunOutboundConnectors(service.store, service.sessions, connector_factory=connector)
    await delivery.resolve(actor.tenant_id, actor.employee_id, command.email)
    assert resolved == [command.email]
    for tenant, employee, address in [
        (new_id("tn"), actor.employee_id, command.email),
        (actor.tenant_id, new_id("emp"), command.email),
        (actor.tenant_id, actor.employee_id, "other@example.com"),
    ]:
        with pytest.raises(ToolGatewayError):
            await delivery.resolve(tenant, employee, address)
    async with service.sessions.begin() as s:
        await s.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == actor.tenant_id,
            EmployeeRow.employee_id == actor.employee_id).values(is_active=False))
    with pytest.raises(ToolGatewayError):
        await delivery.resolve(actor.tenant_id, actor.employee_id, command.email)
    assert resolved == [command.email]
