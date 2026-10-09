"""OAuth 本机存储边界；只使用合成授权材料，不连 Google。"""

import json
from types import SimpleNamespace

import pytest

from connectors.gmail.mailbox_transport import (
    SCOPES,
    GmailMailboxHttpProvider,
    _private_read,
    _save,
)
from shared.schemas.mailbox import MailboxFailure


def test_private_file_rejects_unsafe_modes_and_symlinks(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "credentials.json"
    _save(path, "{}")
    assert _private_read(path) == b"{}"
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(MailboxFailure, match="configuration_invalid"):
        _private_read(path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(MailboxFailure, match="configuration_invalid"):
        _save(link, "{}")


def test_provider_rejects_foreign_token_url_without_any_network(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "credentials.json"
    _save(
        path,
        json.dumps({"token_uri": "https://foreign.example/token", "scopes": SCOPES}),
    )
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **kw: pytest.fail("不允许访问网络"))
    with pytest.raises(MailboxFailure, match="configuration_invalid"):
        GmailMailboxHttpProvider(path)._get("profile", {})


def test_provider_uses_readonly_fixed_google_url_and_redacts_error(
    tmp_path, monkeypatch
):
    tmp_path.chmod(0o700)
    path = tmp_path / "credentials.json"
    _save(
        path,
        json.dumps(
            {"token_uri": "https://oauth2.googleapis.com/token", "scopes": SCOPES}
        ),
    )
    import requests
    from google.oauth2.credentials import Credentials

    monkeypatch.setattr(
        Credentials,
        "from_authorized_user_info",
        lambda *a: SimpleNamespace(valid=True, token="synthetic-only"),
    )
    calls = []

    class Reply:
        status_code = 401

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Reply()

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(MailboxFailure, match="authorization_required") as error:
        GmailMailboxHttpProvider(path)._get("profile", {})
    assert "synthetic-only" not in str(error.value)
    assert calls[0][0] == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    assert calls[0][1]["allow_redirects"] is False
    assert not any("send" in url for url, _ in calls)


@pytest.mark.parametrize(
    "kind,expected",
    [("network", "provider_unavailable"),
     ("retryable", "provider_unavailable"),
     ("revoked", "authorization_required")],
)
def test_refresh_failure_preserves_retryability(tmp_path, monkeypatch, kind, expected):
    """临时刷新失败不能令常驻收信永久退出；撤权仍须重新授权。"""
    import requests
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request

    tmp_path.chmod(0o700)
    path = tmp_path / "credentials.json"
    _save(path, json.dumps({
        "token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "client_id": "synthetic-client",
        "client_secret": "synthetic-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": SCOPES,
        "expiry": "2000-01-01T00:00:00Z",
    }))
    failures = {
        "network": TransportError("synthetic-secret"),
        "retryable": RefreshError("synthetic-secret", retryable=True),
        "revoked": RefreshError("synthetic-secret", retryable=False),
    }

    def failed_request(*args, **kwargs):
        raise failures[kind]

    monkeypatch.setattr(Request, "__call__", failed_request)
    monkeypatch.setattr(requests, "get", lambda *a, **kw: pytest.fail("刷新失败不得抓取"))
    with pytest.raises(MailboxFailure) as error:
        GmailMailboxHttpProvider(path)._get("profile", {})
    assert error.value.code == expected
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("reason,expected,delay", [
    ("rateLimitExceeded", "rate_limited", 60),
    ("userRateLimitExceeded", "rate_limited", 60),
    ("dailyLimitExceeded", "rate_limited", 3600),
    ("insufficientPermissions", "authorization_required", 60),
    ("domainPolicy", "authorization_required", 60),
])
def test_google_403_distinguishes_quota_from_revoked_access(tmp_path, monkeypatch, reason, expected, delay):
    import requests
    from google.oauth2.credentials import Credentials
    tmp_path.chmod(0o700)
    path = tmp_path / "credentials.json"
    _save(path, json.dumps({"token_uri": "https://oauth2.googleapis.com/token", "scopes": SCOPES}))
    monkeypatch.setattr(Credentials, "from_authorized_user_info",
                        lambda *a: SimpleNamespace(valid=True, token="synthetic-only"))

    class Reply:
        status_code = 403
        def __init__(self):
            self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            yield json.dumps({"error": {"errors": [{"reason": reason}],
                                       "message": "private-provider-text"}}).encode()

    monkeypatch.setattr(requests, "get", lambda *a, **kw: Reply())
    with pytest.raises(MailboxFailure) as error:
        GmailMailboxHttpProvider(path)._get("profile", {})
    assert error.value.code == expected
    assert error.value.retry_after == delay
    assert "private-provider-text" not in str(error.value)
