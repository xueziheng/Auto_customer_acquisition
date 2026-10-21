"""原消息页私有测试准备：只建用途与手动单封入组，不发送邮件。"""
from apps.api.identity import RequestIdentity
from domains.conversations.aliyun_connection import AliyunActor
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    EnrollmentCreateRequest,
    EnrollmentExecutionMode,
    EnrollmentOwnershipBinding,
)
from domains.outreach.service import OutreachService
from domains.prospecting.private_reply_test import (
    PrivateReplyTestOptions,
    PrivateReplyTestPrepare,
    PrivateReplyTestPrepared,
)
from infra.db.private_reply_test import PrivateReplyTestStore
from infra.qq_inbound_binding import CurrentQQBinding
from infra.standalone.settings import QQReplySettings
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId


class PrivateReplyTestApplication:
    def __init__(self, settings: QQReplySettings, tenant: TenantId, store: PrivateReplyTestStore, outreach: OutreachService) -> None:
        self._settings, self._tenant, self._store, self._outreach = settings, tenant, store, outreach

    async def prepare(self, identity: RequestIdentity, command: PrivateReplyTestPrepare) -> PrivateReplyTestPrepared:
        employee = identity.employee
        cfg = self._settings
        if (identity.tenant_id != self._tenant or employee.employee_id != cfg.employee_id
            or employee.user_id != cfg.user_id or not employee.is_active):
            raise PermissionDenied("当前账号未获私有回信测试授权")
        binding = CurrentQQBinding(cfg.mailbox_directory,
            AliyunActor(tenant_id=identity.tenant_id, employee_id=employee.employee_id, user_id=employee.user_id),
            cfg.address)
        if not await binding.matches(identity.tenant_id, employee.employee_id, employee.user_id, cfg.address):
            raise PermissionDenied("当前QQ邮箱未连接")
        account, point, locked_at = await self._store.ensure(
            identity.tenant_id, employee.employee_id, employee.user_id, command.sending_identity_id)
        key = await self._store.enrollment_key(identity.tenant_id, account, command.campaign_id)
        scope = OutreachScope(
            level={"boss": ScopeLevel.TENANT, "manager": ScopeLevel.MANAGER, "sales": ScopeLevel.SELF}[employee.role],
            allowed_account_ids=frozenset({account}), allowed_campaign_ids=frozenset({command.campaign_id}))
        enrollment = await self._outreach.enroll(
            identity.tenant_id, command.campaign_id,
            EnrollmentCreateRequest(
                account, point, key,
                ownership=EnrollmentOwnershipBinding(employee.employee_id, locked_at),
                execution_mode=EnrollmentExecutionMode.MANUAL,
            ), actor=Actor(str(employee.employee_id), scope, employee.role))
        return PrivateReplyTestPrepared(account_id=account, contact_point_id=point, enrollment_id=enrollment.enrollment_id)


    async def options(self, identity: RequestIdentity) -> PrivateReplyTestOptions:
        from domains.prospecting.private_reply_test import PrivateReplyTestOptions
        employee = identity.employee
        if (identity.tenant_id != self._tenant or employee.employee_id != self._settings.employee_id
            or employee.user_id != self._settings.user_id or not employee.is_active):
            return PrivateReplyTestOptions()
        return PrivateReplyTestOptions(enabled=True, options=await self._store.options(
            identity.tenant_id, employee.employee_id, employee.user_id))
