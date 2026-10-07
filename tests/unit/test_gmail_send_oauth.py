"""发信授权只使用独立私有文件，并核验 Gmail 账号。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from connectors.gmail.mailbox_transport import SCOPES as READ_SCOPES
from connectors.gmail.mailbox_transport import _private_read, _save
from connectors.gmail.send_oauth import (
    SEND_SCOPES,
    GmailOAuthTokenSource,
    GmailSendAuthorizationError,
    authorize_local_send,
)


def _client(path):
    _save(
        path,
        json.dumps(
            {
                "installed": {
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "test-client",
                    "client_secret": "test-secret",
                }
            }
        ),
    )


def test_send_authorization_keeps_readonly_credentials_untouched(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    client, existing = tmp_path / "client.json", tmp_path / "readonly.json"
    _client(client)
    _save(existing, json.dumps({"scopes": READ_SCOPES, "marker": "unchanged"}))
    from google_auth_oauthlib import flow

    monkeypatch.setattr(
        flow.InstalledAppFlow,
        "from_client_config",
        lambda *args, **kwargs: pytest.fail("不得打开授权页面"),
    )
    with pytest.raises(GmailSendAuthorizationError):
        authorize_local_send(client, existing)
    assert json.loads(_private_read(existing))["marker"] == "unchanged"


def test_send_authorization_requests_only_read_and_send(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    client, credentials = tmp_path / "client.json", tmp_path / "send.json"
    _client(client)
    observed = {}

    class Flow:
        def run_local_server(self, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(
                scopes=SEND_SCOPES,
                granted_scopes=SEND_SCOPES,
                refresh_token="synthetic-refresh",
                to_json=lambda: json.dumps(
                    {"scopes": SEND_SCOPES, "token_uri": "https://oauth2.googleapis.com/token"}
                ),
            )

    from google_auth_oauthlib import flow

    def from_config(config, scopes, **kwargs):
        assert set(scopes) == set(SEND_SCOPES)
        assert kwargs["autogenerate_code_verifier"] is True
        return Flow()

    monkeypatch.setattr(flow.InstalledAppFlow, "from_client_config", from_config)
    authorize_local_send(client, credentials, browser="chrome")
    assert observed["host"] == "127.0.0.1"
    assert observed["access_type"] == "offline"
    assert observed["browser"] == "chrome"
    assert credentials.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("granted,refresh", [(READ_SCOPES, "synthetic-refresh"), (SEND_SCOPES, None)])
def test_authorization_rejects_incomplete_or_nonrenewable_grant(
    tmp_path, monkeypatch, granted, refresh
):
    """申请的 scope 不等于实际授权；不可续期的授权也不能报告成功。"""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib import flow

    tmp_path.chmod(0o700)
    client, destination = tmp_path / "client.json", tmp_path / "send.json"
    _client(client)
    credentials = Credentials(
        token="synthetic-token", refresh_token=refresh,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="test-client", client_secret="test-secret",
        scopes=SEND_SCOPES, granted_scopes=granted,
    )
    monkeypatch.setattr(
        flow.InstalledAppFlow, "from_client_config",
        lambda *a, **kw: SimpleNamespace(run_local_server=lambda **kw: credentials),
    )
    with pytest.raises(GmailSendAuthorizationError):
        authorize_local_send(client, destination)
    assert not destination.exists()


def test_token_source_rejects_wrong_scope_before_network(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    credentials = tmp_path / "send.json"
    _save(
        credentials,
        json.dumps(
            {"token_uri": "https://oauth2.googleapis.com/token", "scopes": READ_SCOPES}
        ),
    )
    import requests

    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: pytest.fail("不得访问网络"))
    with pytest.raises(GmailSendAuthorizationError):
        GmailOAuthTokenSource(credentials, "owner@example.com").token()


def test_token_source_checks_actual_google_account(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    credentials = tmp_path / "send.json"
    _save(
        credentials,
        json.dumps(
            {"token_uri": "https://oauth2.googleapis.com/token", "scopes": SEND_SCOPES}
        ),
    )
    import requests
    from google.oauth2.credentials import Credentials

    monkeypatch.setattr(
        Credentials,
        "from_authorized_user_info",
        lambda *args: SimpleNamespace(valid=True, token="synthetic-token"),
    )
    calls = []

    class Reply:
        status_code = 200

        def iter_content(self, size):
            assert size == 4096
            yield b'{"emailAddress":"different@example.com"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Reply()

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(GmailSendAuthorizationError) as error:
        GmailOAuthTokenSource(credentials, "owner@example.com").token()
    assert "synthetic-token" not in str(error.value)
    assert (
        GmailOAuthTokenSource(credentials, "different@example.com").token()
        == "synthetic-token"
    )
    assert calls[0][0] == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    assert calls[0][1]["allow_redirects"] is False


@pytest.mark.parametrize("failure", [None, "refresh", "profile", "oversized", "malformed"])
def test_expired_token_refresh_and_provider_failures(tmp_path, monkeypatch, failure):
    """用真实 Google Credentials 验证刷新、私有落盘与固定错误边界。"""
    import requests

    tmp_path.chmod(0o700)
    path = tmp_path / "send.json"
    original = json.dumps({
        "token": "synthetic-expired", "refresh_token": "synthetic-refresh",
        "client_id": "test-client", "client_secret": "test-secret",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": SEND_SCOPES,
        "expiry": "2000-01-01T00:00:00Z",
    })
    _save(path, original)

    def request(session, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        if url == "https://oauth2.googleapis.com/token":
            if failure == "refresh":
                raise requests.ConnectionError("synthetic-provider-sensitive-response")
            response._content = json.dumps({
                "access_token": "synthetic-refreshed", "expires_in": 3600,
                "token_type": "Bearer", "scope": " ".join(SEND_SCOPES),
            }).encode()
        elif url == "https://gmail.googleapis.com/gmail/v1/users/me/profile":
            assert kwargs["headers"]["Authorization"] == "Bearer synthetic-refreshed"
            assert kwargs["allow_redirects"] is False
            response.status_code = 403 if failure == "profile" else 200
            response._content = (
                b"x" * 4097 if failure == "oversized" else b"not-json"
                if failure == "malformed" else b'{"emailAddress":"owner@example.com"}'
            )
        else:
            pytest.fail("不得请求其他服务")
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    source = GmailOAuthTokenSource(path, "owner@example.com")
    if failure:
        from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

        with pytest.raises(ToolGatewayError) as error:
            source.token()
        if failure == "refresh":
            assert error.value.category is ToolErrorCategory.PROVIDER_TRANSIENT
            assert error.value.is_retryable
            assert _private_read(path).decode() == original
        else:
            assert error.value.category is ToolErrorCategory.PROVIDER_AUTH_REQUIRED
            assert str(error.value) == "gmail_send_authorization_required"
        assert "synthetic-provider-sensitive-response" not in str(error.value)
    else:
        assert source.token() == "synthetic-refreshed"
        assert json.loads(_private_read(path))["token"] == "synthetic-refreshed"
        assert path.stat().st_mode & 0o777 == 0o600
