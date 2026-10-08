"""固定租户的 Postgres 认证；不持有外部凭证、不创建业务员工。"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.authentication.passwords import (
    csrf_for,
    hash_password,
    hash_test_password,
    password_work,
    token_digest,
    verify_password,
)
from infra.db.tables import (
    AuthAccountRow,
    AuthRateLimitRow,
    AuthSessionRow,
    EmployeeRow,
)
from shared.authentication import (
    AuthenticationDenied,
    AuthenticationInputInvalid,
    AuthenticationRateLimited,
    AuthPrincipal,
    IssuedSession,
    normalize_login_username,
)
from shared.schemas.identifiers import EmployeeId, TenantId, UserId

_DUMMY = SecretStr("scrypt$131072$8$1$" + "00" * 16 + "$" + "00" * 32)


class PostgresAuthentication:
    """租户绑定认证适配器；所有管理操作仅供可信本机装配调用。

    登录尝试另行提交；随后租户桶锁、账号行锁按同一顺序持有至发行或撤销提交。
    本机试点有意串行同租户登录与管理，以换取清晰的撤销语义。
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], tenant_id: TenantId
    ) -> None:
        self._factory = session_factory
        self._tenant_id = tenant_id

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self._factory.begin() as session:
                yield session
        except SQLAlchemyError:
            raise AuthenticationDenied() from None

    async def _now(self, session: AsyncSession) -> datetime:
        # 使用数据库实际时钟，行锁等待不缩短限流窗口或错判到期。
        result = await session.scalar(select(func.clock_timestamp()))
        assert isinstance(result, datetime)
        return result

    async def _bucket(self, session: AsyncSession, bucket: str) -> AuthRateLimitRow:
        await session.execute(
            insert(AuthRateLimitRow)
            .values(
                tenant_id=self._tenant_id,
                bucket=bucket,
                count=0,
                started_at=func.clock_timestamp(),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "bucket"])
        )
        row = await session.scalar(
            select(AuthRateLimitRow)
            .where(
                AuthRateLimitRow.tenant_id == self._tenant_id,
                AuthRateLimitRow.bucket == bucket,
            )
            .with_for_update()
        )
        assert row is not None
        return row

    async def _account(
        self, session: AsyncSession, username: str
    ) -> AuthAccountRow | None:
        return await session.scalar(
            select(AuthAccountRow)
            .where(
                AuthAccountRow.tenant_id == self._tenant_id,
                AuthAccountRow.username == username,
            )
            .with_for_update()
        )

    async def _employee(
        self, session: AsyncSession, employee_id: str
    ) -> EmployeeRow | None:
        return await session.scalar(
            select(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == self._tenant_id,
                EmployeeRow.employee_id == employee_id,
            )
            .with_for_update(read=True)
        )

    def _principal(self, employee: EmployeeRow) -> AuthPrincipal:
        if not employee.is_active or not employee.user_id:
            raise AuthenticationDenied()
        return AuthPrincipal(
            tenant_id=self._tenant_id,
            employee_id=EmployeeId(employee.employee_id),
            user_id=UserId(employee.user_id),
        )

    async def _attempt(self) -> None:
        limited = False
        async with self._transaction() as session:
            bucket = await self._bucket(session, "attempts")
            now = await self._now(session)
            if now >= bucket.started_at + timedelta(minutes=1):
                bucket.count, bucket.started_at = 0, now
            if bucket.count >= 30:
                limited = True
            else:
                bucket.count += 1
        if limited:
            raise AuthenticationRateLimited()

    async def login(self, username: str, password: SecretStr) -> IssuedSession:
        """限流计数失败也提交；每次发行新随机会话，至多保留五个活动会话。"""
        await self._attempt()
        try:
            username = normalize_login_username(username)
        except AuthenticationInputInvalid:
            username = ""
        failure: AuthenticationDenied | AuthenticationRateLimited | None = None
        issued: IssuedSession | None = None
        async with self._transaction() as session:
            await self._bucket(session, "attempts")
            account = (
                await self._account(session, username)
                if self._valid_username(username)
                else None
            )
            now = await self._now(session)
            unknown = (
                await self._bucket(session, "unknown") if account is None else None
            )
            if account is not None:
                count, started = account.failed_count, account.failure_started_at
            else:
                assert unknown is not None
                count, started = unknown.count, unknown.started_at
            if started is None or now >= started + timedelta(minutes=15):
                count, started = 0, now
            if count >= 5:
                failure = AuthenticationRateLimited()
            else:
                # 未知/停用路径也运行同参数 scrypt，错误不区分账号存在性。
                record = (
                    SecretStr(account.password_hash)
                    if account and account.enabled
                    else _DUMMY
                )
                matched = await password_work(verify_password, password, record)
                employee = (
                    await self._employee(session, account.employee_id)
                    if account
                    else None
                )
                if (
                    not matched
                    or account is None
                    or not account.enabled
                    or employee is None
                    or not employee.is_active
                    or not employee.user_id
                ):
                    count += 1
                    failure = AuthenticationDenied()
                else:
                    count, started = 0, None
                    now = await self._now(session)
                    token = SecretStr(secrets.token_urlsafe(32))
                    csrf = csrf_for(token)
                    issued = IssuedSession(
                        principal=self._principal(employee),
                        token=token,
                        csrf_token=csrf,
                        expires_at=now + timedelta(hours=8),
                    )
                    active = list(
                        await session.scalars(
                            select(AuthSessionRow)
                            .where(
                                AuthSessionRow.tenant_id == self._tenant_id,
                                AuthSessionRow.username == username,
                                AuthSessionRow.revoked_at.is_(None),
                                AuthSessionRow.expires_at > now,
                            )
                            .order_by(
                                AuthSessionRow.created_at, AuthSessionRow.token_digest
                            )
                        )
                    )
                    for old in active[: max(0, len(active) - 4)]:
                        old.revoked_at = now
                    session.add(
                        AuthSessionRow(
                            tenant_id=self._tenant_id,
                            token_digest=token_digest(token),
                            csrf_digest=token_digest(csrf),
                            username=username,
                            user_id=employee.user_id,
                            account_version=account.version,
                            created_at=now,
                            expires_at=issued.expires_at,
                            revoked_at=None,
                        )
                    )
            if account is not None:
                account.failed_count, account.failure_started_at = count, started
            elif unknown is not None:
                assert started is not None
                unknown.count, unknown.started_at = count, started
        if failure is not None:
            raise failure
        assert issued is not None
        return issued

    async def _read_session(
        self, token: SecretStr, csrf_token: SecretStr | None = None
    ) -> IssuedSession:
        try:
            digest = token_digest(token)
            csrf_digest = token_digest(csrf_token) if csrf_token is not None else None
        except AuthenticationInputInvalid:
            raise AuthenticationDenied() from None
        async with self._transaction() as session:
            # 单条读取使用同一快照关联当前账号和员工，GET 路径没有写入。
            result = (
                await session.execute(
                    select(AuthSessionRow, AuthAccountRow, EmployeeRow)
                    .join(
                        AuthAccountRow,
                        (AuthAccountRow.tenant_id == AuthSessionRow.tenant_id)
                        & (AuthAccountRow.username == AuthSessionRow.username),
                    )
                    .join(
                        EmployeeRow,
                        (EmployeeRow.tenant_id == AuthAccountRow.tenant_id)
                        & (EmployeeRow.employee_id == AuthAccountRow.employee_id),
                    )
                    .where(
                        AuthSessionRow.tenant_id == self._tenant_id,
                        AuthAccountRow.tenant_id == self._tenant_id,
                        EmployeeRow.tenant_id == self._tenant_id,
                        AuthSessionRow.token_digest == digest,
                        AuthSessionRow.revoked_at.is_(None),
                        AuthSessionRow.expires_at > func.clock_timestamp(),
                        AuthAccountRow.enabled.is_(True),
                        AuthAccountRow.version == AuthSessionRow.account_version,
                        EmployeeRow.is_active.is_(True),
                        EmployeeRow.user_id == AuthSessionRow.user_id,
                    )
                )
            ).one_or_none()
            if result is None:
                raise AuthenticationDenied()
            row, _, employee = result
            if csrf_digest is not None and not hmac.compare_digest(
                row.csrf_digest, csrf_digest
            ):
                raise AuthenticationDenied()
            csrf = csrf_for(token)
            if not hmac.compare_digest(row.csrf_digest, token_digest(csrf)):
                raise AuthenticationDenied()
            return IssuedSession(
                principal=self._principal(employee),
                token=token,
                csrf_token=csrf,
                expires_at=row.expires_at,
            )

    async def authenticate(
        self, token: SecretStr, *, csrf_token: SecretStr | None = None
    ) -> AuthPrincipal:
        """每次解析检查当前启用状态、用户映射、版本与到期；可检查会话绑定 CSRF。"""
        return (await self._read_session(token, csrf_token)).principal

    async def get_session(self, token: SecretStr) -> IssuedSession:
        """只读恢复原会话与派生 CSRF，不续期或旋转材料。"""
        return await self._read_session(token)

    async def logout(self, token: SecretStr) -> None:
        """幂等撤销精确租户会话；格式无效的 token 视为已退出。"""
        try:
            digest = token_digest(token)
        except AuthenticationInputInvalid:
            return
        async with self._transaction() as session:
            await session.execute(
                update(AuthSessionRow)
                .where(
                    AuthSessionRow.tenant_id == self._tenant_id,
                    AuthSessionRow.token_digest == digest,
                    AuthSessionRow.revoked_at.is_(None),
                )
                .values(revoked_at=func.clock_timestamp())
            )

    @staticmethod
    def _valid_username(username: str) -> bool:
        try:
            normalize_login_username(username)
            return True
        except AuthenticationInputInvalid:
            return False

    async def create_account(
        self,
        username: str,
        password: SecretStr,
        employee_id: EmployeeId,
        *,
        session: AsyncSession | None = None,
        test_password: bool = False,
    ) -> AuthPrincipal:
        """绑定现有活跃员工；可接收可信本机装配的事务以原子创建员工与账号。

        外部 session 必须已开启事务，调用方负责提交或回滚；本方法只 flush。
        允许调用方先插入随机新 ID 的 Employee 再调用，未提交的新行不可能被登录锁住。
        不允许调用前锁住或修改既有 Employee/账号而反转租户→账号→员工锁序。
        不创建员工、不接受角色；租户由实例固定，不由传入 session 推断。
        """
        username = normalize_login_username(username)
        if session is not None and not session.in_transaction():
            raise AuthenticationInputInvalid()
        record = await password_work(hash_test_password if test_password else hash_password, password)

        async def bind(target: AsyncSession) -> AuthPrincipal:
            await self._bucket(target, "attempts")
            employee = await self._employee(target, employee_id)
            if employee is None or not employee.is_active or not employee.user_id:
                raise AuthenticationInputInvalid()
            target.add(
                AuthAccountRow(
                    tenant_id=self._tenant_id,
                    username=username,
                    employee_id=employee_id,
                    password_hash=record.get_secret_value(),
                    enabled=True,
                    version=1,
                    failed_count=0,
                    failure_started_at=None,
                )
            )
            await target.flush()
            return self._principal(employee)

        try:
            if session is not None:
                return await bind(session)
            async with self._factory.begin() as owned:
                return await bind(owned)
        except SQLAlchemyError:
            raise AuthenticationInputInvalid() from None

    async def _revoke(self, session: AsyncSession, username: str | None) -> None:
        statement = update(AuthSessionRow).where(
            AuthSessionRow.tenant_id == self._tenant_id,
            AuthSessionRow.revoked_at.is_(None),
        )
        if username is not None:
            statement = statement.where(AuthSessionRow.username == username)
        await session.execute(statement.values(revoked_at=func.clock_timestamp()))

    async def reset_password(self, username: str, password: SecretStr) -> None:
        """可信本机重置密码；同事务增加版本、清失败计数并撤销全部旧会话。"""
        username = normalize_login_username(username)
        record = await password_work(hash_password, password)
        async with self._transaction() as session:
            await self._bucket(session, "attempts")
            account = await self._account(session, username)
            if account is None:
                raise AuthenticationInputInvalid()
            account.password_hash = record.get_secret_value()
            account.version += 1
            account.failed_count, account.failure_started_at = 0, None
            await self._revoke(session, username)

    async def set_enabled(self, username: str, enabled: bool) -> None:
        """可信本机启停账号；每次变更版本并撤销会话，重新启用不复活旧会话。"""
        username = normalize_login_username(username)
        async with self._transaction() as session:
            await self._bucket(session, "attempts")
            account = await self._account(session, username)
            if account is None:
                raise AuthenticationInputInvalid()
            account.enabled = enabled
            account.version += 1
            await self._revoke(session, username)

    async def revoke_all(self, *, username: str | None = None) -> None:
        """撤销指定账号或本实例租户全部会话；与登录发行共享租户锁。

        username=None 用于静止环境恢复后撤销。完成后新的正确密码登录仍可发行。
        """
        if username is not None:
            username = normalize_login_username(username)
        async with self._transaction() as session:
            await self._bucket(session, "attempts")
            await self._revoke(session, username)
