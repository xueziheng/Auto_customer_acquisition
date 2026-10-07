"""登录身份与会话契约；材料仅供可信传输层使用，禁止写入日志或模型。"""

import re
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from shared.schemas.identifiers import EmployeeId, TenantId, UserId


class AuthenticationDenied(Exception):
    """统一认证拒绝，不泄露账号是否存在或停用。"""

    def __init__(self) -> None:
        super().__init__("账号或密码错误，或会话已失效")


class AuthenticationRateLimited(Exception):
    """持久化登录限流。"""

    def __init__(self) -> None:
        super().__init__("尝试次数过多，请稍后重试")


class AuthenticationInputInvalid(Exception):
    """可信管理输入拒绝；不携带原输入与数据库异常。"""

    def __init__(self) -> None:
        super().__init__("账号资料不符合要求")


def normalize_login_username(username: str) -> str:
    """兼容原用户名与 ASCII 邮箱登录；不合并邮箱点号或加号别名。"""
    if not isinstance(username, str) or not username.isascii():
        raise AuthenticationInputInvalid()
    if "@" not in username:
        if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", username) is None:
            raise AuthenticationInputInvalid()
        return username
    if len(username) > 254 or username.count("@") != 1:
        raise AuthenticationInputInvalid()
    normalized = username.lower()
    local, domain = normalized.split("@")
    atom = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (
        not 1 <= len(local) <= 64
        or re.fullmatch(atom + r"(?:\." + atom + ")*", local) is None
        or re.fullmatch(label + r"(?:\." + label + ")+", domain) is None
    ):
        raise AuthenticationInputInvalid()
    return normalized


class AuthPrincipal(BaseModel):
    """身份仅带租户、员工与用户映射；权限由当前业务事实解析。"""

    model_config = ConfigDict(frozen=True)
    tenant_id: TenantId
    employee_id: EmployeeId
    user_id: UserId


class IssuedSession(BaseModel):
    """绝对到期的会话；默认序列化和 repr 均排除认证材料。"""

    model_config = ConfigDict(frozen=True)
    principal: AuthPrincipal
    token: SecretStr = Field(repr=False, exclude=True)
    csrf_token: SecretStr = Field(repr=False, exclude=True)
    expires_at: datetime


class AuthenticationService(Protocol):
    """固定租户的认证服务，管理功能不暴露给业务 Agent。"""

    async def login(self, username: str, password: SecretStr) -> IssuedSession:
        """验证密码并发放新会话，失败计数必须持久化。"""
        ...

    async def authenticate(
        self, token: SecretStr, *, csrf_token: SecretStr | None = None
    ) -> AuthPrincipal:
        """读取当前账号及员工映射；指定 CSRF 时同时校验。"""
        ...

    async def get_session(self, token: SecretStr) -> IssuedSession:
        """只读恢复会话 CSRF 与原到期时间，不续期、不产生写操作。"""
        ...

    async def logout(self, token: SecretStr) -> None:
        """幂等撤销本租户指定会话。"""
        ...
