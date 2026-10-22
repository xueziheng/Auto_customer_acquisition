"""阿里邮箱凭证仅由受信连接器读取，状态投影不能泄露凭证。"""
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr


def test_private_binding_is_actor_scoped_and_status_never_contains_password(tmp_path):
    from domains.conversations.aliyun_connection import AliyunActor
    from infra.aliyun_mail_store import AliyunBinding, AliyunMailStore

    tmp_path.chmod(0o700)
    store = AliyunMailStore(tmp_path)
    actor = AliyunActor(tenant_id="tn_a", employee_id="emp_a", user_id="usr_a")
    binding = AliyunBinding(actor=actor, email="sales@example.com",
        password=SecretStr("synthetic-test-only"), connected_at=datetime.now(UTC), operation_id="aco_test")
    store.publish(binding)
    assert store.binding(actor).password.get_secret_value() == "synthetic-test-only"
    assert "synthetic-test-only" not in repr(binding)
    assert store.binding(actor.model_copy(update={"employee_id": "emp_b"})) is None
    assert store.binding(actor.model_copy(update={"tenant_id": "tn_b"})) is None
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in tmp_path.iterdir())


def test_connect_rejects_headers_and_secrets_are_redacted():
    from domains.conversations.aliyun_connection import AliyunConnectCommand

    with pytest.raises(ValueError):
        AliyunConnectCommand(email="sales@example.com\r\nBcc:x@example.com",
            password="synthetic-test-only", confirm_connect=True)
    command = AliyunConnectCommand(email="sales@example.com",
        password="synthetic-test-only", confirm_connect=True)
    assert "synthetic-test-only" not in repr(command)
    assert "synthetic-test-only" not in command.model_dump_json()
    with pytest.raises(ValueError):
        AliyunConnectCommand(email="sales@example.com", password="synthetic-test-only", confirm_connect=1)
