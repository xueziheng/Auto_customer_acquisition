"""普通来信当前QQ连接版本检查；密钥只供Gateway执行时的连接器解析。"""

from domains.conversations.aliyun_connection import AliyunActor
from domains.conversations.business_mail import BusinessMailActor
from infra.aliyun_mail_store import AliyunMailStore
from shared.errors import PermissionDenied


class CurrentBusinessMailboxBinding:
    def __init__(self, store: AliyunMailStore) -> None:
        self._store = store

    async def check(
        self, actor: BusinessMailActor, address: str, operation: str
    ) -> None:
        binding = self._store.binding(AliyunActor(**actor.model_dump()))
        if (
            binding is None
            or binding.email != address
            or binding.operation_id != operation
        ):
            raise PermissionDenied("当前邮箱连接已撤销或轮换")

    def resolver(
        self, actor: BusinessMailActor, address: str, operation: str
    ) -> "BusinessMailboxSecretResolver":
        return BusinessMailboxSecretResolver(self._store, actor, address, operation)


class BusinessMailboxSecretResolver:
    def __init__(
        self,
        store: AliyunMailStore,
        actor: BusinessMailActor,
        address: str,
        operation: str,
    ) -> None:
        self._store, self._actor, self._address, self._operation = (
            store,
            actor,
            address,
            operation,
        )

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "BUSINESS_QQ_MAIL":
            raise PermissionDenied("普通收件凭证范围无效")
        binding = self._store.binding(AliyunActor(**self._actor.model_dump()))
        if (
            binding is None
            or binding.email != self._address
            or binding.operation_id != self._operation
        ):
            raise PermissionDenied("当前邮箱连接已撤销或轮换")
        return binding.password.get_secret_value()
