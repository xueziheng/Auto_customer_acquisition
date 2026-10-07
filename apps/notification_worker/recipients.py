"""notification worker 私有、租户绑定且 repr 隐藏的收件人目录。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from shared.errors import PolicyViolation, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT = re.compile(rf"tn_{_ULID}\Z")
_EMPLOYEE = re.compile(rf"emp_{_ULID}\Z")
_MAILBOX = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
_KEYS = frozenset({"tenant_id", "employee_id", "address"})
_CREDENTIAL_MARKERS = (
    "bearer",
    "token",
    "secret",
    "password",
    "authorization",
    "akia",
    "sk-",
)
_MAX_RECIPIENTS = 1_000


@dataclass(frozen=True, repr=False)
class NotificationRecipient:
    tenant_id: TenantId
    employee_id: EmployeeId
    address: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_tenant(self.tenant_id) or not _valid_employee(self.employee_id):
            raise ValidationError("通知收件人无效")
        _validate_mailbox(self.address)


@runtime_checkable
class NotificationRecipientDirectory(Protocol):
    async def resolve(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> NotificationRecipient: ...


class ConfiguredNotificationRecipientDirectory:
    """仅由 worker composition 注入；不进入 API 依赖或任何持久记录。"""

    def __init__(self, recipients: Sequence[NotificationRecipient]) -> None:
        if (
            not isinstance(recipients, Sequence)
            or isinstance(recipients, (str, bytes, bytearray))
            or not 1 <= len(recipients) <= _MAX_RECIPIENTS
            or any(not isinstance(item, NotificationRecipient) for item in recipients)
        ):
            raise ValidationError("通知收件人目录无效")
        entries: dict[tuple[TenantId, EmployeeId], NotificationRecipient] = {}
        for recipient in recipients:
            key = (recipient.tenant_id, recipient.employee_id)
            if key in entries:
                raise ValidationError("通知收件人目录无效")
            entries[key] = recipient
        self._entries = entries

    def __repr__(self) -> str:
        return "ConfiguredNotificationRecipientDirectory()"

    @classmethod
    def from_value(cls, value: object) -> ConfiguredNotificationRecipientDirectory:
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= _MAX_RECIPIENTS
        ):
            raise ValidationError("通知收件人目录配置无效")
        recipients: list[NotificationRecipient] = []
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != _KEYS:
                raise ValidationError("通知收件人目录配置无效")
            if any(
                not isinstance(key, str)
                or any(marker in key.casefold() for marker in _CREDENTIAL_MARKERS)
                for key in entry
            ):
                raise ValidationError("通知收件人目录配置无效")
            tenant = entry.get("tenant_id")
            employee = entry.get("employee_id")
            address = entry.get("address")
            if (
                not isinstance(tenant, str)
                or not isinstance(employee, str)
                or not isinstance(address, str)
                or not _valid_tenant(tenant)
                or not _valid_employee(employee)
            ):
                raise ValidationError("通知收件人目录配置无效")
            try:
                recipients.append(
                    NotificationRecipient(
                        TenantId(tenant),
                        EmployeeId(employee),
                        address,
                    )
                )
            except (TypeError, ValidationError):
                raise ValidationError("通知收件人目录配置无效") from None
        return cls(recipients)

    async def resolve(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> NotificationRecipient:
        if not _valid_tenant(tenant_id) or not _valid_employee(employee_id):
            raise ValidationError("通知收件人查询无效")
        recipient = self._entries.get((tenant_id, employee_id))
        if recipient is None:
            raise PolicyViolation("通知收件人未配置")
        return recipient


def _valid_tenant(value: object) -> bool:
    return isinstance(value, str) and _TENANT.fullmatch(value) is not None


def _valid_employee(value: object) -> bool:
    return isinstance(value, str) and _EMPLOYEE.fullmatch(value) is not None


def _validate_mailbox(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > 254
        or _MAILBOX.fullmatch(value) is None
        or any(marker in value.casefold() for marker in _CREDENTIAL_MARKERS)
    ):
        raise ValidationError("通知收件地址无效")
    local_part, _domain = value.rsplit("@", 1)
    if local_part.startswith(".") or local_part.endswith(".") or ".." in local_part:
        raise ValidationError("通知收件地址无效")
