"""QQ固定端点及实际邮件协议适配；不调用真实邮箱。"""
import ssl
from dataclasses import replace

import pytest
from pydantic import SecretStr

from connectors.qq_mail.client import QQMailConnector, QQMailTransport
from shared.errors import ValidationError
from tests.unit.test_aliyun_mail_connector import MailServer, request


async def test_qq_single_message_and_mismatched_sender(monkeypatch):
    monkeypatch.setattr("tests.unit.test_aliyun_mail_connector.ADDRESS", "fixture@qq.com")
    monkeypatch.setattr("tests.unit.test_aliyun_mail_connector.PASSWORD", SecretStr("synthetic-only"))
    server = MailServer()
    connector = QQMailConnector("fixture@qq.com", SecretStr("synthetic-only"), transport=server)
    await connector.authenticate()
    assert server.sent == []
    with pytest.raises(ValidationError):
        await connector.send_once(replace(request(), from_address="wrong@example.com"))
    result = await connector.send_once(replace(request(), from_address="fixture@qq.com"))
    assert result.provider_ref.startswith("qq_")
    assert len(server.sent) == 1


def test_qq_transport_is_fixed_tls(monkeypatch):
    calls = []
    def capture(*args, **kwargs):
        calls.append((args, kwargs))
        return object()
    monkeypatch.setattr("connectors.qq_mail.client.smtplib.SMTP_SSL", capture)
    monkeypatch.setattr("connectors.qq_mail.client.BoundedIMAP4SSL", capture)
    QQMailTransport().open_smtp()
    QQMailTransport().open_imap()
    assert [entry[0] for entry in calls] == [("smtp.qq.com", 465), ("imap.qq.com", 993)]
    for _, options in calls:
        context = options.get("context", options.get("ssl_context"))
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    with pytest.raises(ValidationError):
        QQMailConnector("fixture@example.com", SecretStr("synthetic-only"))
