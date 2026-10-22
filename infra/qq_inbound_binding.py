"""只读使用现有QQ私有连接；构造不触碰文件或解析凭证。"""
from __future__ import annotations

from pathlib import Path

from domains.conversations.aliyun_connection import AliyunActor
from infra.aliyun_mail_store import AliyunMailStore
from shared.schemas.email_inbound import InboundError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId


class CurrentQQBinding:
    def __init__(self, root: Path, actor: AliyunActor, address: str) -> None:
        self._root, self._actor, self._address = root, actor, address

    def _binding(self):
        try:
            binding = AliyunMailStore(self._root).binding(self._actor)
            if binding is None or binding.email != self._address:
                raise InboundError()
            return binding
        except Exception:  # noqa: BLE001 私有连接错误不回显凭证或文件内容
            raise InboundError() from None

    async def matches(
        self, tenant_id: TenantId, employee_id: EmployeeId, user_id: UserId,
        address: str,
    ) -> bool:
        if (tenant_id, employee_id, user_id, address) != (
            self._actor.tenant_id, self._actor.employee_id,
            self._actor.user_id, self._address,
        ):
            return False
        self._binding()
        return True

    def resolve(self, secret_ref: str) -> str:
        """仅由QQ连接器在Gateway执行阶段调用；不回退到任意密钥引用。"""
        if secret_ref != "QQ_INBOUND_AUTH":
            raise InboundError()
        return self._binding().password.get_secret_value()
