"""失效的用户映射不得进入 Gmail provider；不连接数据库或真实网络。"""
from datetime import UTC, datetime

import pytest

from domains.conversations.gmail_connection import GmailActor
from infra.gmail_web_store import GmailBinding, GmailWebStore
from shared.errors import PermissionDenied
from shared.schemas.identifiers import new_id
from tool_gateway.fingerprint import HmacFingerprintProvider


async def test_revoked_user_stops_before_provider(tmp_path, monkeypatch):
    import apps.email_feedback_worker.web_mailbox as module
    tmp_path.chmod(0o700)
    actor = GmailActor(tenant_id=new_id("tn"), employee_id=new_id("emp"), user_id=new_id("usr"))
    binding = GmailBinding(actor=actor, email="synthetic@gmail.com", mailbox_id=new_id("mbx"),
                           operation_id=new_id("gco"), connected_at=datetime.now(UTC))

    class Revoked:
        def __init__(self, sessions):
            pass
        async def role(self, value):
            assert value == actor
            raise PermissionDenied("员工映射已撤销")

    def forbidden(*args, **kwargs):
        pytest.fail("失效身份不得构造 Gmail provider")
    monkeypatch.setattr(module, "SqlGmailWebAccess", Revoked)
    monkeypatch.setattr(module, "GmailWebMailboxHttpProvider", forbidden)
    with pytest.raises(PermissionDenied):
        await module.sync_binding(object(), object(), GmailWebStore(tmp_path), binding,
                                  HmacFingerprintProvider("v1", b"x" * 32))
