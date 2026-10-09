"""固定 Google 地址的网页授权交换；授权码、PKCE 与凭证均不进入审计结果。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from connectors.gmail.mailbox_transport import (
    GmailMailboxHttpProvider,
    _private_read,
    _save,
)
from connectors.gmail.send_oauth import SEND_SCOPES, GmailSendAuthorizationError


class GmailWebOAuth:
    def __init__(self, client_file: Path, redirect_uri: str):
        self._client_file, self._redirect_uri = client_file, redirect_uri

    def _flow(self, state: SecretStr, verifier: SecretStr) -> Any:
        import google_auth_oauthlib.flow as google_flow  # type: ignore[import-untyped]

        config = json.loads(_private_read(self._client_file))
        web = config["web"]
        if (set(config) != {"web"}
                or web["auth_uri"] != "https://accounts.google.com/o/oauth2/auth"
                or web["token_uri"] != "https://oauth2.googleapis.com/token"
                or not web.get("client_id") or not web.get("client_secret")
                or self._redirect_uri not in web.get("redirect_uris", [])
                or not self._redirect_uri.startswith("https://")):
            raise ValueError()
        flow = google_flow.Flow.from_client_config(
            config, SEND_SCOPES, state=state.get_secret_value(),
            code_verifier=verifier.get_secret_value(), redirect_uri=self._redirect_uri,
        )
        flow.oauth2session.trust_env = False
        return flow

    def authorization_url(self, state: SecretStr, verifier: SecretStr, email: str) -> str:
        try:
            flow = self._flow(state, verifier)
            url, returned_state = flow.authorization_url(
                access_type="offline", prompt="consent", login_hint=email,
                include_granted_scopes="false",
            )
            if returned_state != state.get_secret_value():
                raise ValueError()
            return str(url)
        except Exception:  # noqa: BLE001 - 私有授权边界只返回固定安全错误
            raise GmailSendAuthorizationError() from None

    def exchange(self, code: SecretStr, verifier: SecretStr, state: SecretStr,
                 destination: Path, expected_email: str) -> None:
        """只由 Gateway 执行；成功前核验实际 scope、refresh token 和 Google profile。"""
        try:
            import requests

            flow = self._flow(state, verifier)
            from oauthlib.oauth2 import (
                WebApplicationClient,  # type: ignore[import-untyped]
            )

            with requests.Session() as exchange_session:
                exchange_session.trust_env = False
                with exchange_session.post(
                    "https://oauth2.googleapis.com/token",
                    data={"grant_type": "authorization_code",
                          "client_id": flow.client_config["client_id"],
                          "client_secret": flow.client_config["client_secret"],
                          "redirect_uri": self._redirect_uri,
                          "code": code.get_secret_value(),
                          "code_verifier": verifier.get_secret_value()},
                    timeout=20, allow_redirects=False, stream=True,
                ) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    parts, length = [], 0
                    for part in response.iter_content(4096):
                        length += len(part)
                        if length > 16384:
                            raise ValueError()
                        parts.append(part)
                    raw = b"".join(parts).decode()
            flow.oauth2session.token = WebApplicationClient(
                flow.client_config["client_id"]
            ).parse_request_body_response(raw, scope=SEND_SCOPES)
            credentials = flow.credentials
            if (set(credentials.scopes or ()) != set(SEND_SCOPES)
                    or set(flow.oauth2session.token.get("scope", ())) != set(SEND_SCOPES)
                    or (credentials.granted_scopes is not None
                        and set(credentials.granted_scopes) != set(SEND_SCOPES))
                    or not credentials.refresh_token or not credentials.token):
                raise ValueError()
            with requests.Session() as session:
                session.trust_env = False
                with session.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                    headers={"Authorization": "Bearer " + credentials.token},
                    timeout=20, allow_redirects=False, stream=True,
                ) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    chunks, size = [], 0
                    for chunk in response.iter_content(4096):
                        size += len(chunk)
                        if size > 4096:
                            raise ValueError()
                        chunks.append(chunk)
                    profile = json.loads(b"".join(chunks))
            if (not isinstance(profile, dict)
                    or str(profile.get("emailAddress", "")).casefold() != expected_email):
                raise ValueError()
            _save(destination, credentials.to_json())
        except Exception:  # noqa: BLE001 - 私有授权边界只返回固定安全错误
            raise GmailSendAuthorizationError() from None


class GmailWebMailboxHttpProvider(GmailMailboxHttpProvider):
    """网页用户明确授予 read+send；镜像入口仍只执行 GET，旧只读配置不放宽。"""

    def _required_scopes(self) -> list[str]:
        return list(SEND_SCOPES)
