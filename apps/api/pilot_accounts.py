"""可信本机账号维护；只接显式租户与连接工厂，profile 由运行入口装配。"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys
from dataclasses import dataclass
from typing import Literal, Never

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.employees.schemas import EmployeeView
from domains.employees.service import validate_employee_provisioning
from infra.authentication.service import PostgresAuthentication
from infra.db.tables import EmployeeRow
from shared.authentication import AuthenticationInputInvalid
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, new_id


@dataclass(frozen=True)
class AccountCommand:
    """无凭证命令；停用仅影响可登录状态，不隐式停用业务员工。"""

    action: Literal["create", "reset-password", "enable", "disable"]
    username: str
    name: str | None = None
    role: str | None = None
    manager_id: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", self.username, re.ASCII) is None:
            raise AuthenticationInputInvalid()
        if self.action == "create":
            if self.role is None or self.name is None or self.manager_id == "":
                raise AuthenticationInputInvalid()
        elif self.action not in {"reset-password", "enable", "disable"} or any(
            value is not None for value in (self.name, self.role, self.manager_id)
        ):
            raise AuthenticationInputInvalid()


async def run_account_command(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    command: AccountCommand,
    *,
    password: SecretStr | None = None,
) -> None:
    """可信操作者命令；新员工与账号同事务，失败完整回滚，不创建业务审批。

    create 只写随机新员工 ID，不预锁既有员工；账号服务持有租户→账号锁后再校验经理。
    enable/disable 只改变登录状态；重置和停用由认证服务原子撤销会话。
    """
    if not tenant_id or tenant_id != tenant_id.strip():
        raise AuthenticationInputInvalid()
    auth = PostgresAuthentication(session_factory, tenant_id)
    if command.action in {"create", "reset-password"} and password is None:
        raise AuthenticationInputInvalid()
    if command.action not in {"create", "reset-password"} and password is not None:
        raise AuthenticationInputInvalid()
    if command.action == "create":
        try:
            validate_employee_provisioning(
                tenant_id,
                name=command.name or "",
                role=command.role or "",
                manager=None,
            )
        except ValidationError:
            raise AuthenticationInputInvalid() from None
        assert password is not None
        employee_id = EmployeeId(new_id("emp"))
        try:
            async with session_factory.begin() as session:
                session.add(
                    EmployeeRow(
                        tenant_id=tenant_id,
                        employee_id=employee_id,
                        user_id=new_id("usr"),
                        name=command.name,
                        role=command.role,
                        is_active=True,
                        manager_id=None,
                    )
                )
                await auth.create_account(
                    command.username, password, employee_id, session=session
                )
                if command.manager_id is not None:
                    manager = await session.scalar(
                        select(EmployeeRow)
                        .where(
                            EmployeeRow.tenant_id == tenant_id,
                            EmployeeRow.employee_id == command.manager_id,
                        )
                        .with_for_update()
                    )
                    if manager is None:
                        raise AuthenticationInputInvalid()
                    validate_employee_provisioning(
                        tenant_id,
                        name=command.name or "",
                        role=command.role or "",
                        manager=EmployeeView(
                            tenant_id=TenantId(manager.tenant_id),
                            employee_id=EmployeeId(manager.employee_id),
                            name=manager.name,
                            role=manager.role,
                            is_active=manager.is_active,
                        ),
                    )
                    employee = await session.scalar(
                        select(EmployeeRow).where(
                            EmployeeRow.tenant_id == tenant_id,
                            EmployeeRow.employee_id == employee_id,
                        )
                    )
                    if employee is None:
                        raise AuthenticationInputInvalid()
                    employee.manager_id = command.manager_id
        except (SQLAlchemyError, ValidationError):
            raise AuthenticationInputInvalid() from None
    elif command.action == "reset-password":
        assert password is not None
        await auth.reset_password(command.username, password)
    else:
        await auth.set_enabled(command.username, command.action == "enable")


class _SafeParser(argparse.ArgumentParser):
    """未知参数不得回显，尤其不能把误传密码输出到终端。"""

    def error(self, message: str) -> Never:
        del message
        raise AuthenticationInputInvalid()


def parse_command(argv: list[str]) -> AccountCommand:
    """解析无密码参数；profile 配置加载由外层运行入口负责。"""
    parser = _SafeParser(
        description="可信本机账号维护；停用账号不改变员工业务状态", allow_abbrev=False
    )
    commands = parser.add_subparsers(
        dest="action", required=True, parser_class=_SafeParser
    )
    for action in ("create", "reset-password", "enable", "disable"):
        child = commands.add_parser(action, allow_abbrev=False)
        child.add_argument("--username", required=True)
        if action == "create":
            child.add_argument("--name", required=True)
            child.add_argument("--role", required=True)
            child.add_argument("--manager-id")
    return AccountCommand(**vars(parser.parse_args(argv)))


def read_password() -> SecretStr:
    """仅在真实交互终端用 getpass 读两次；不接受管道或回显降级。"""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AuthenticationInputInvalid()
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            first = SecretStr(getpass.getpass("设置密码："))
            second = SecretStr(getpass.getpass("再次输入密码："))
    except (getpass.GetPassWarning, EOFError, OSError):
        raise AuthenticationInputInvalid() from None
    if first != second:
        raise AuthenticationInputInvalid()
    return first


def run_interactive(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    argv: list[str],
) -> int:
    """供 profile 入口接线：解析后交互读密码，只输出固定状态。"""
    try:
        command = parse_command(argv)
        password = (
            read_password() if command.action in {"create", "reset-password"} else None
        )
        asyncio.run(
            run_account_command(session_factory, tenant_id, command, password=password)
        )
    except Exception:  # noqa: BLE001 可信终端不得输出凭证或底层参数
        print("账号操作失败（account_command_failed）", file=sys.stderr)
        return 1
    print("账号操作完成")
    return 0


def main(argv: list[str] | None = None) -> int:
    """profile 接线前失败关闭；不新建第二套数据库配置格式。"""
    try:
        parse_command(sys.argv[1:] if argv is None else argv)
    except AuthenticationInputInvalid:
        print("账号参数无效（account_input_invalid）", file=sys.stderr)
        return 2
    print("本机 profile 尚未接线（account_profile_not_configured）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
