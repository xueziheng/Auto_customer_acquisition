"""本机本人邮箱账号创建与追加绑定；密码仅在真实 TTY 输入。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Never

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.pilot_accounts import AccountCommand, read_password, run_account_command
from infra.authentication.service import PostgresAuthentication
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.db.tables import AuthAccountRow
from infra.pilot.config import PilotError
from infra.pilot.mailbox_config import MailboxBinding, MailboxConfig
from shared.authentication import normalize_login_username
from shared.schemas.identifiers import TenantId


async def configure_account(
    profile: Path,
    sessions: async_sessionmaker[AsyncSession],
    *,
    username: str,
    password: SecretStr,
    email: str,
    credentials_file: Path,
) -> None:
    """只建真实只读账号；追加绑定必须验证同一账号的当前密码。"""
    config = MailboxConfig.read(profile)
    tenant = TenantId(config.tenant_id)
    username = normalize_login_username(username)
    # 先校验路径和地址形状，避免无效绑定先留下一个不能使用的账号。
    email = MailboxBinding.valid_email(email)
    credentials_file = MailboxBinding.absolute_credentials(credentials_file)
    async with sessions() as db:
        existing = await db.scalar(select(AuthAccountRow.employee_id).where(
            AuthAccountRow.tenant_id == tenant, AuthAccountRow.username == username,
        ))
    if existing is None:
        await run_account_command(
            sessions, tenant,
            AccountCommand("create", username, name=username, role="viewer"),
            password=password,
        )
    auth = PostgresAuthentication(sessions, tenant)
    issued = await auth.login(username, password)
    try:
        MailboxConfig.bind(
            profile, employee_id=issued.principal.employee_id,
            email=email, credentials_file=credentials_file,
        )
    finally:
        await auth.logout(issued.token)


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise PilotError("account_input_invalid")


async def _run(args: argparse.Namespace, password: SecretStr) -> None:
    config = MailboxConfig.read(args.profile)
    engine = create_engine_from(config.database_url.get_secret_value())
    try:
        await assert_database_schema_current(engine)
        await configure_account(
            args.profile, async_sessionmaker(engine, expire_on_commit=False),
            username=args.username, password=password, email=args.email,
            credentials_file=args.credentials_file,
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = SafeParser(description="本人邮箱账号设置；不接受密码参数", allow_abbrev=False)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    try:
        args = parser.parse_args()
        print("设置 TradeOS 登录密码（至少 15 个字符）。已有账号请输入当前密码。")
        password = read_password()
        asyncio.run(_run(args, password))
    except Exception:  # noqa: BLE001 不输出底层参数、密码或邮箱资料
        print("账号或邮箱绑定未完成（account_setup_failed）", file=sys.stderr)
        return 1
    print("账号与邮箱绑定已保存。重新启动邮箱服务后开始同步。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
