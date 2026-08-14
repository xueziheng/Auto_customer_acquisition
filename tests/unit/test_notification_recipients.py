"""事务通知收件人目录的租户与敏感数据边界。"""

from __future__ import annotations

import importlib
import json

import pytest

from shared.errors import PolicyViolation, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, new_id


def _module():
    try:
        return importlib.import_module("apps.notification_worker.recipients")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：通知收件人目录尚未实现（{exc}）")


def _entry(
    tenant: TenantId, employee: EmployeeId, address: str = "owner@example.com"
) -> dict[str, str]:
    return {
        "tenant_id": str(tenant),
        "employee_id": str(employee),
        "address": address,
    }


@pytest.mark.asyncio
async def test_directory_resolves_only_exact_tenant_employee_pair_and_hides_address() -> None:
    module = _module()
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    directory = module.ConfiguredNotificationRecipientDirectory.from_value(
        [_entry(tenant, employee)]
    )

    recipient = await directory.resolve(tenant, employee)
    assert recipient.tenant_id == tenant
    assert recipient.employee_id == employee
    assert recipient.address == "owner@example.com"
    assert "owner@example.com" not in repr(recipient)
    assert "owner@example.com" not in repr(directory)

    for requested_tenant, requested_employee in (
        (TenantId(new_id("tn")), employee),
        (tenant, EmployeeId(new_id("emp"))),
    ):
        with pytest.raises(PolicyViolation) as caught:
            await directory.resolve(requested_tenant, requested_employee)
        assert "owner@example.com" not in str(caught.value)


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"tenant_id": "bad", "employee_id": "bad", "address": "x@example.com"}],
        [
            {
                "tenant_id": "tn_01H00000000000000000000000",
                "employee_id": "emp_01H00000000000000000000000",
                "address": "Display <owner@example.com>",
            }
        ],
        [
            {
                "tenant_id": "tn_01H00000000000000000000000",
                "employee_id": "emp_01H00000000000000000000000",
                "address": "token-owner@example.com",
            }
        ],
        [
            {
                "tenant_id": "tn_01H00000000000000000000000",
                "employee_id": "emp_01H00000000000000000000000",
                "address": "owner@example.com",
                "password": "private-marker",
            }
        ],
    ],
)
def test_directory_rejects_malformed_or_credential_shaped_config_without_echo(
    entries: object,
) -> None:
    module = _module()
    with pytest.raises(ValidationError) as caught:
        module.ConfiguredNotificationRecipientDirectory.from_value(entries)
    rendered = str(caught.value).casefold()
    for marker in ("owner@example.com", "private-marker", "password", "token-owner"):
        assert marker not in rendered


def test_directory_rejects_duplicate_pairs_and_unbounded_config() -> None:
    module = _module()
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    duplicate = [_entry(tenant, employee), _entry(tenant, employee, "other@example.com")]
    with pytest.raises(ValidationError):
        module.ConfiguredNotificationRecipientDirectory.from_value(duplicate)

    entries = [
        _entry(tenant, EmployeeId(new_id("emp")), f"owner{index}@example.com")
        for index in range(1001)
    ]
    with pytest.raises(ValidationError):
        module.ConfiguredNotificationRecipientDirectory.from_value(entries)


@pytest.mark.asyncio
async def test_directory_rejects_noncanonical_lookup_without_echo() -> None:
    module = _module()
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    directory = module.ConfiguredNotificationRecipientDirectory.from_value(
        [_entry(tenant, employee)]
    )
    for requested_tenant, requested_employee in (
        (TenantId("bad-tenant"), employee),
        (tenant, EmployeeId("bad-employee")),
    ):
        with pytest.raises(ValidationError) as caught:
            await directory.resolve(requested_tenant, requested_employee)
        assert str(requested_tenant) not in str(caught.value)
        assert str(requested_employee) not in str(caught.value)


def test_worker_email_settings_are_explicit_bounded_and_repr_hidden() -> None:
    config_module = importlib.import_module("apps.notification_worker.config")
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    identity = new_id("sid")
    environ = {
        "DATABASE_URL": "postgresql+asyncpg://",
        "TRADEOS_TENANT_ID": str(tenant),
        "TRADEOS_NOTIFICATION_POLL_INTERVAL_SECONDS": "5",
        "TRADEOS_NOTIFICATION_BATCH_LIMIT": "20",
        "TRADEOS_NOTIFICATION_HEALTH_PORT": "8093",
        "TRADEOS_NOTIFICATION_LEASE_OWNER": "notification-worker-1",
        "TRADEOS_NOTIFICATION_GMAIL_BASE_URL": "https://gmail.googleapis.com",
        "TRADEOS_NOTIFICATION_SENDING_IDENTITY_ID": identity,
        "TRADEOS_NOTIFICATION_RECIPIENTS_JSON": json.dumps(
            [_entry(tenant, employee)], separators=(",", ":")
        ),
        "GMAIL_OAUTH_TOKEN_REF": "NOTIFICATION_GMAIL_OAUTH_VALUE",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "NOTIFICATION_FINGERPRINT_VALUE",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "notification-v1",
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TRADEOS_DEV_MODE": "false",
        "NOTIFICATION_GMAIL_OAUTH_VALUE": "oauth-private-marker",
        "NOTIFICATION_FINGERPRINT_VALUE": "f" * 32,
    }
    config = config_module.NotificationWorkerConfig.from_environ(environ)
    assert config.email is not None
    assert config.email.sending_identity_id == identity
    rendered = repr(config)
    for marker in (
        "owner@example.com",
        "oauth-private-marker",
        "NOTIFICATION_GMAIL_OAUTH_VALUE",
        "worker:private",
    ):
        assert marker not in rendered

    for missing in (
        "TRADEOS_NOTIFICATION_GMAIL_BASE_URL",
        "TRADEOS_NOTIFICATION_SENDING_IDENTITY_ID",
        "TRADEOS_NOTIFICATION_RECIPIENTS_JSON",
        "GMAIL_OAUTH_TOKEN_REF",
        "TOOL_CALL_FINGERPRINT_KEY_REF",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION",
        "TRADEOS_TOOL_LEASE_SECONDS",
        "TRADEOS_DEV_MODE",
    ):
        invalid = dict(environ)
        del invalid[missing]
        with pytest.raises(config_module.NotificationWorkerConfigurationError):
            config_module.NotificationWorkerConfig.from_environ(invalid)
