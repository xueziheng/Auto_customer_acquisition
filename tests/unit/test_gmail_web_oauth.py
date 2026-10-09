"""使用真实 OAuth 库，仅替换网络边界；错误账号和部分授权不得落盘。"""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from connectors.gmail.mailbox_transport import _private_read, _save
from connectors.gmail.send_oauth import SEND_SCOPES, GmailSendAuthorizationError

REDIRECT = "https://trade.example.com/inbox/mailbox/google-callback"


def client(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "client.json"
    _save(path, json.dumps({"web": {
        "client_id": "synthetic-client", "client_secret": "synthetic-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [REDIRECT],
    }}))
    return path


def test_web_url_uses_exact_callback_and_pkce(tmp_path):
    from connectors.gmail.web_oauth import GmailWebOAuth
    flow = GmailWebOAuth(client(tmp_path), REDIRECT)
    state, verifier = SecretStr("s" * 43), SecretStr("v" * 64)
    url = flow.authorization_url(state, verifier, "owner@gmail.com")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.hostname == "accounts.google.com"
    assert query["redirect_uri"] == [REDIRECT]
    assert query["state"] == ["s" * 43]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(b"v" * 64).digest()).rstrip(b"=").decode()
    ]
    assert set(query["scope"][0].split()) == set(SEND_SCOPES)
    assert query["access_type"] == ["offline"]
    assert "synthetic-secret" not in url
    with pytest.raises(GmailSendAuthorizationError):
        GmailWebOAuth(client(tmp_path), "https://evil.example/callback").authorization_url(state, verifier, "owner@gmail.com")


@pytest.mark.parametrize("failure", [None, "wrong_account", "partial", "no_refresh", "network", "oversized"])
def test_exchange_verifies_scopes_and_account_before_private_save(tmp_path, monkeypatch, failure):
    import requests

    from connectors.gmail.web_oauth import GmailWebOAuth
    path = client(tmp_path)
    output = tmp_path / "credentials.json"
    called = []

    def request(session, method, url, **kwargs):
        called.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        if url == "https://oauth2.googleapis.com/token":
            assert kwargs["allow_redirects"] is False
            assert kwargs["timeout"] == 20
            if failure == "network":
                raise requests.ConnectionError("private-provider-error")
            payload = {"access_token": "synthetic-access", "token_type": "Bearer",
                       "expires_in": 3600, "scope": " ".join(SEND_SCOPES)}
            if failure != "no_refresh":
                payload["refresh_token"] = "synthetic-refresh"
            if failure == "partial":
                payload["scope"] = SEND_SCOPES[0]
            response._content = json.dumps(payload).encode()
        elif url == "https://gmail.googleapis.com/gmail/v1/users/me/profile":
            assert kwargs["headers"]["Authorization"] == "Bearer synthetic-access"
            assert kwargs["allow_redirects"] is False
            response._content = (b"x" * 4097 if failure == "oversized" else
                json.dumps({"emailAddress": "wrong@gmail.com" if failure == "wrong_account" else "owner@gmail.com"}).encode())
        else:
            pytest.fail("不得访问非固定 Google 服务")
        return response
    monkeypatch.setattr(requests.Session, "request", request)
    flow = GmailWebOAuth(path, REDIRECT)
    args = (SecretStr("synthetic-code"), SecretStr("v" * 64), SecretStr("s" * 43), output, "owner@gmail.com")
    if failure:
        with pytest.raises(GmailSendAuthorizationError) as error:
            flow.exchange(*args)
        assert "synthetic" not in str(error.value)
        assert "private-provider" not in str(error.value)
        assert not output.exists()
    else:
        flow.exchange(*args)
        stored = json.loads(_private_read(output))
        assert stored["refresh_token"] == "synthetic-refresh"
        assert set(stored["scopes"]) == set(SEND_SCOPES)
        assert output.stat().st_mode & 0o777 == 0o600
