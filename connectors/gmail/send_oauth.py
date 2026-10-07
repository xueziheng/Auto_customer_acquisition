"""本机 Gmail 发信授权与短期访问令牌刷新；凭证不离开 Connector。"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Protocol

from connectors.gmail.mailbox_transport import SCOPES as READ_SCOPES
from connectors.gmail.mailbox_transport import _private_read, _save
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

SEND_SCOPES = (*READ_SCOPES, "https://www.googleapis.com/auth/gmail.send")
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


class GmailSendAuthorizationError(ToolGatewayError):
    """固定错误；不得把 Google 响应、凭证或邮箱内容带出连接器。"""

    def __init__(self) -> None:
        super().__init__(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        self.args = ("gmail_send_authorization_required",)


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


def authorize_local_send(
    client_file: Path, credentials_file: Path, *, browser: str | None = None
) -> None:
    """单独申请读取与发送权限，不覆盖已有只读授权。"""
    try:
        import google_auth_oauthlib.flow as google_flow  # type: ignore[import-untyped]

        config = json.loads(_private_read(client_file))
        installed = config["installed"]
        if (
            installed["auth_uri"] != _AUTH_URI
            or installed["token_uri"] != _TOKEN_URI
            or client_file == credentials_file
            or browser not in {None, "chrome"}
        ):
            raise ValueError()
        if credentials_file.exists():
            old = json.loads(_private_read(credentials_file))
            if set(old.get("scopes", ())) != set(SEND_SCOPES):
                raise ValueError()
        flow = google_flow.InstalledAppFlow.from_client_config(
            config, SEND_SCOPES, autogenerate_code_verifier=True
        )
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            authorization_prompt_message="请在 Google 页面确认 Gmail 读取和发送权限。",
            success_message="Gmail 发信授权已完成，可以关闭本页。",
            timeout_seconds=300,
            access_type="offline",
            prompt="consent",
            browser=browser,
        )
        if (
            set(credentials.scopes or ()) != set(SEND_SCOPES)
            or (
                credentials.granted_scopes is not None
                and set(credentials.granted_scopes) != set(SEND_SCOPES)
            )
            or not credentials.refresh_token
        ):
            raise ValueError()
        _save(credentials_file, credentials.to_json())
    except Exception:  # noqa: BLE001 - 外部授权错误不能泄露凭证或响应
        raise GmailSendAuthorizationError() from None


class GmailOAuthTokenSource:
    """仅在 Gateway 放行后的 Connector 调用中解析并刷新授权文件。"""

    def __init__(self, credentials_file: Path, expected_email: str) -> None:
        if not expected_email or expected_email != expected_email.strip().casefold():
            raise GmailSendAuthorizationError()
        self._file = credentials_file
        self._expected_email = expected_email
        self._lock = Lock()

    def token(self) -> str:
        try:
            import requests
            from google.auth.exceptions import RefreshError, TransportError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError:
            raise GmailSendAuthorizationError() from None
        with self._lock:
            try:
                data = json.loads(_private_read(self._file))
                if data.get("token_uri") != _TOKEN_URI or set(
                    data.get("scopes", ())
                ) != set(SEND_SCOPES):
                    raise ValueError()
                credentials = Credentials.from_authorized_user_info(
                    data, SEND_SCOPES
                )
                if not credentials.valid:
                    credentials.refresh(Request())
                    _save(self._file, credentials.to_json())
                token = credentials.token
                if not isinstance(token, str) or not token:
                    raise ValueError()
                with requests.get(
                    _PROFILE_URL,
                    headers={"Authorization": "Bearer " + token},
                    timeout=10,
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    chunks, size = [], 0
                    for chunk in response.iter_content(4096):
                        size += len(chunk)
                        if size > 4096:
                            raise ValueError()
                        chunks.append(chunk)
                    payload = b"".join(chunks)
                profile = json.loads(payload)
                if not isinstance(profile, dict) or profile.get(
                    "emailAddress", ""
                ).casefold() != self._expected_email:
                    raise ValueError()
                return token
            except ToolGatewayError:
                raise
            except (TransportError, requests.RequestException):
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
            except RefreshError as error:
                if error.retryable:
                    raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None
                raise GmailSendAuthorizationError() from None
            except Exception:  # noqa: BLE001 - 仅固定错误可离开凭证边界
                raise GmailSendAuthorizationError() from None


class GmailOAuthSecretResolver:
    """保留原有密钥引用，只将 Gmail 引用映射到本机 OAuth 文件。"""

    def __init__(self, delegate: SecretResolver, source: GmailOAuthTokenSource):
        self._delegate = delegate
        self._source = source

    def resolve(self, secret_ref: str) -> str:
        if secret_ref == "GMAIL_OAUTH_TOKEN_REF":
            return self._source.token()
        return self._delegate.resolve(secret_ref)
