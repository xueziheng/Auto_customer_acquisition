"""本人邮箱镜像的独立只读入口；不启动旧反馈或任何发送流程。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.gmail.mailbox import GmailMailboxReader
from connectors.gmail.mailbox_transport import GmailMailboxHttpProvider, authorize_local
from domains.conversations.mailbox import MailboxActor
from infra.db.advisory_lock import PostgresAdvisoryLock, derive_advisory_lock_key
from infra.db.mailbox import SqlMailboxRepository
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import PilotConfig
from infra.pilot.mailbox_config import MailboxConfig
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id
from shared.schemas.mailbox import MailboxFailure, MailboxPage
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.mailbox import (
    MANIFEST,
    MailboxFetchHandler,
    MailboxSlot,
    PageReader,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork


class _TenantCheck:
    name = "tenant"

    def __init__(self, actor: MailboxActor, mailbox_id: str):
        self.actor, self.mailbox_id = actor, mailbox_id

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if (
            ctx.tenant_id != self.actor.tenant_id
            or ctx.params.get("mailbox_id") != self.mailbox_id
            or ctx.tool_id != MANIFEST.tool_id
        ):
            return CheckRejection(
                "tenant", "tenant:resource_binding", "邮箱同步范围无效"
            )
        return None


class MailboxSync:
    """先审计再访问 Provider；锁心跳成功后原子提交整页。"""

    def __init__(
        self,
        repository: SqlMailboxRepository,
        actor: MailboxActor,
        mailbox_id: str,
        reader: PageReader,
        fingerprints: HmacFingerprintProvider,
        lock: PostgresAdvisoryLock,
    ):
        self.repository, self.actor, self.mailbox_id, self.lock = (
            repository,
            actor,
            mailbox_id,
            lock,
        )
        self.slot = MailboxSlot()
        registry = ToolRegistry()
        registry.register(
            MANIFEST, MailboxFetchHandler(reader, self.slot, fingerprints)
        )
        self.user_id = UserId(new_id("usr"))

        async def allowed(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
            current = await repository.checkpoint(actor, mailbox_id)
            return (
                ctx.user_id == self.user_id
                and ctx.tenant_id == actor.tenant_id
                and ctx.params.get("cursor") == current.cursor
                and await lock.heartbeat()
            )

        self.gateway = ToolGateway(
            registry,  # type: ignore[arg-type]
            {
                "tenant": _TenantCheck(actor, mailbox_id),
                "permission": PermissionCheck(allowed),
            },
            lambda tenant: cast(
                ToolGatewayUnitOfWork,
                SqlAlchemyToolGatewayUnitOfWork(repository.sessions, tenant),
            ),
            lease_duration=timedelta(minutes=20),
            lease_owner="private-mailbox",
            now=lambda: datetime.now(UTC),
            id_factory=new_id,
        )

    async def once(self) -> MailboxPage:
        if not await self.lock.heartbeat():
            raise MailboxFailure()
        expected = await self.repository.checkpoint(self.actor, self.mailbox_id)
        try:
            result = await self.gateway.invoke(
                ToolCallContext(
                    tenant_id=self.actor.tenant_id,
                    user_id=self.user_id,
                    tool_id=MANIFEST.tool_id,
                    params={"mailbox_id": self.mailbox_id, "cursor": expected.cursor},
                )
            )
            if result.status is not ToolCallStatus.SUCCEEDED:
                error = self.slot.failure() or MailboxFailure()
                await self.repository.failed(self.actor, self.mailbox_id, error.code)
                raise error
            value = self.slot.take(str((result.output or {}).get("provider_ref", "")))
            if not isinstance(value, MailboxPage) or not await self.lock.heartbeat():
                raise MailboxFailure()
            await self.repository.apply_page(self.actor, expected, value)
            return value
        finally:
            self.slot.clear()


@dataclass(frozen=True)
class SyncConfiguration:
    """进程内窄配置，秘密只用于仓储连接和 Gateway 指纹。"""

    database_url: SecretStr
    tenant_id: str
    employee_id: str
    email: str
    credentials_file: Path
    fingerprint_version: str
    fingerprint_key: SecretStr


def load_sync_configuration(args: argparse.Namespace) -> SyncConfiguration:
    """旧完整 profile 与新邮箱 profile 互斥，禁止命令行覆盖已绑定归属。"""
    if getattr(args, "mailbox_profile", None) is not None:
        if any(getattr(args, key, None) is not None for key in (
            "profile", "employee_id", "email", "credentials_file"
        )):
            raise MailboxFailure("configuration_invalid")
        config = MailboxConfig.read(args.mailbox_profile)
        binding = next((item for item in config.bindings
                        if item.binding_id == args.binding_id), None)
        if binding is None:
            raise MailboxFailure("configuration_invalid")
        return SyncConfiguration(
            config.database_url, config.tenant_id, binding.employee_id,
            binding.email, binding.credentials_file, "mailbox-local-v1",
            config.fingerprint_key,
        )
    if getattr(args, "binding_id", None) is not None or not all(
        getattr(args, key, None) is not None
        for key in ("profile", "employee_id", "email", "credentials_file")
    ):
        raise MailboxFailure("configuration_invalid")
    profile = PilotConfig.read(args.profile)
    env = profile.runtime_environment()
    return SyncConfiguration(
        profile.database_url, profile.tenant_id, args.employee_id, args.email,
        args.credentials_file, env["TOOL_CALL_FINGERPRINT_KEY_VERSION"],
        SecretStr(profile.resolve(env["TOOL_CALL_FINGERPRINT_KEY_REF"])),
    )


async def run(args: argparse.Namespace) -> int:
    config = load_sync_configuration(args)
    engine = create_engine_from(config.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = MailboxActor(
        tenant_id=TenantId(config.tenant_id), employee_id=EmployeeId(config.employee_id)
    )
    repository = SqlMailboxRepository(sessions)
    lock = None
    try:
        await assert_database_schema_current(engine)
        mailbox_id = await repository.register(actor, config.email)
        lock = PostgresAdvisoryLock(
            engine, derive_advisory_lock_key(actor.tenant_id, "mailbox:" + mailbox_id)
        )
        if not await lock.acquire():
            print(json.dumps({"status": "already_running"}))
            return 0
        sync = MailboxSync(
            repository,
            actor,
            mailbox_id,
            GmailMailboxReader(
                GmailMailboxHttpProvider(config.credentials_file), config.email
            ),
            HmacFingerprintProvider(
                config.fingerprint_version,
                config.fingerprint_key.get_secret_value().encode(),
            ),
            lock,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        while not stop.is_set():
            try:
                page = await sync.once()
                print(
                    json.dumps(
                        {"status": page.phase, "processed": len(page.messages)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if page.phase != "synced":
                    continue
                if not args.watch:
                    return 0
                delay = args.interval
                allow_requested_wakeup = True
            except MailboxFailure as error:
                print(
                    json.dumps({"status": "failed", "reason": error.code}), flush=True
                )
                if not args.watch or error.code not in {
                    "rate_limited",
                    "provider_unavailable",
                }:
                    return 2
                delay = error.retry_after
                allow_requested_wakeup = False
            # 完整页后才响应终止；显式刷新可唤醒普通轮询，但不绕过 Provider 限流。
            deadline = loop.time() + delay
            while not stop.is_set() and loop.time() < deadline:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=min(5, deadline - loop.time())
                    )
                except TimeoutError:
                    if (
                        allow_requested_wakeup
                        and (
                            await repository.checkpoint(actor, mailbox_id)
                        ).sync_requested
                    ):
                        break
        return 0
    finally:
        if lock is not None:
            await lock.close()
        await engine.dispose()


def main() -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="本人 Gmail 只读同步")
    commands = parser.add_subparsers(dest="command", required=True)
    connect = commands.add_parser("authorize")
    connect.add_argument("--client-file", type=Path, required=True)
    connect.add_argument("--credentials-file", type=Path, required=True)
    sync = commands.add_parser("sync")
    profiles = sync.add_mutually_exclusive_group(required=True)
    profiles.add_argument("--profile", type=Path)
    profiles.add_argument("--mailbox-profile", type=Path)
    sync.add_argument("--binding-id")
    sync.add_argument("--employee-id")
    sync.add_argument("--email")
    sync.add_argument("--credentials-file", type=Path)
    sync.add_argument("--watch", action="store_true")
    sync.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    try:
        if args.command == "authorize":
            authorize_local(args.client_file, args.credentials_file)
            print(json.dumps({"status": "authorized", "scope": "readonly"}))
            return 0
        if not 15 <= args.interval <= 3600:
            raise MailboxFailure("configuration_invalid")
        return asyncio.run(run(args))
    except Exception:  # noqa: BLE001 - 凭证和 Provider 原文不得越过安全边界
        print(json.dumps({"status": "failed", "reason": "mailbox_setup_required"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
