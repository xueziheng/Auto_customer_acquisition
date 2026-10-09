"""独立 Gmail 只读 OAuth；凭证仅在 connector 内解析和刷新。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from shared.schemas.mailbox import MailboxFailure

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _private_read(path: Path) -> bytes:
    """拒绝链接、非本人文件及宽松权限；错误不含路径或文件正文。"""
    try:
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError()
        directory = path.parent.stat()
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if (
                info.st_uid != os.getuid()
                or info.st_nlink != 1
                or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError()
            value = file.read(65537)
            if len(value) > 65536:
                raise ValueError()
            return value
    except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
        raise MailboxFailure("configuration_invalid") from None


def _save(path: Path, content: str) -> None:
    """同目录原子替换；不允许覆盖链接目标。"""
    temporary = None
    try:
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError()
        if path.exists():
            _private_read(path)
        directory = path.parent.stat()
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise ValueError()
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".gmail-")
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
        raise MailboxFailure("configuration_invalid") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def authorize_local(client_file: Path, credentials_file: Path) -> None:
    """操作者运行本机连接向导；只读 scope，localhost 回调，PKCE 与随机 state。"""
    try:
        import google_auth_oauthlib.flow as google_flow  # type: ignore[import-untyped]

        config = json.loads(_private_read(client_file))
        installed = config["installed"]
        if (
            installed["auth_uri"] != "https://accounts.google.com/o/oauth2/auth"
            or installed["token_uri"] != "https://oauth2.googleapis.com/token"
        ):
            raise ValueError()
        flow = google_flow.InstalledAppFlow.from_client_config(
            config, SCOPES, autogenerate_code_verifier=True
        )
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            authorization_prompt_message="请在打开的 Google 页面选择邮箱并完成只读授权。",
            success_message="邮箱只读授权已完成，可以关闭本页。",
            timeout_seconds=300,
            access_type="offline",
            prompt="consent",
        )
        _save(credentials_file, credentials.to_json())
    except MailboxFailure:
        raise
    except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
        raise MailboxFailure("authorization_required") from None


class GmailMailboxHttpProvider:
    def __init__(self, credentials_file: Path):
        self._file = credentials_file

    def _required_scopes(self) -> list[str]:
        return list(SCOPES)

    async def get(self, path: str, params: dict[str, str]) -> dict[str, object]:
        return await asyncio.to_thread(self._get, path, params)

    def _get(self, path: str, params: dict[str, str]) -> dict[str, object]:
        if (
            re.fullmatch(r"profile|messages|history|messages/[a-f0-9]{1,64}", path)
            is None
        ):
            raise MailboxFailure("configuration_invalid")
        try:
            import requests
            from google.auth.exceptions import RefreshError, TransportError
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            data = json.loads(_private_read(self._file))
            if data.get("token_uri") != "https://oauth2.googleapis.com/token" or set(
                data.get("scopes", [])
            ) != set(self._required_scopes()):
                raise MailboxFailure("configuration_invalid")
            credentials = Credentials.from_authorized_user_info(data, self._required_scopes())
            if not credentials.valid:
                try:
                    credentials.refresh(Request())
                except TransportError:
                    raise MailboxFailure("provider_unavailable") from None
                except RefreshError as error:
                    raise MailboxFailure(
                        "provider_unavailable" if error.retryable else "authorization_required"
                    ) from None
                except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
                    raise MailboxFailure("authorization_required") from None
                _save(self._file, credentials.to_json())
            with requests.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/" + path,
                params=params,
                headers={"Authorization": "Bearer " + credentials.token},
                timeout=30,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code in {401, 403}:
                    raise MailboxFailure("authorization_required")
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After", "60")
                    raise MailboxFailure(
                        "rate_limited", int(retry) if retry.isdigit() else 60
                    )
                if response.status_code == 404:
                    raise MailboxFailure(
                        "history_expired" if path == "history" else "message_deleted"
                    )
                if response.status_code != 200:
                    raise MailboxFailure()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise MailboxFailure("invalid_response")
                    chunks.append(chunk)
                result = json.loads(b"".join(chunks))
                if not isinstance(result, dict):
                    raise MailboxFailure("invalid_response")
                return result
        except MailboxFailure:
            raise
        except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
            raise MailboxFailure() from None
