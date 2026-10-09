"""云端本人 Gmail 镜像模式：显式租户配置、逐邮箱锁与独立退避，无发信端口。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.email_feedback_worker.mailbox import MailboxSync
from connectors.gmail.mailbox import GmailMailboxReader
from connectors.gmail.web_oauth import GmailWebMailboxHttpProvider
from domains.conversations.mailbox import MailboxActor
from infra.db.advisory_lock import PostgresAdvisoryLock, derive_advisory_lock_key
from infra.db.gmail_web_access import SqlGmailWebAccess
from infra.db.mailbox import SqlMailboxRepository
from infra.db.runtime_scope import verify_runtime_database_scope
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.gmail_web_store import GmailBinding, GmailWebStore
from infra.pilot.config import PilotConfig
from infra.standalone.gmail_settings import load_gmail_settings
from shared.errors import PermissionDenied
from shared.schemas.mailbox import MailboxFailure, MailboxPage
from tool_gateway.fingerprint import HmacFingerprintProvider


async def sync_binding(engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession],
                       store: GmailWebStore, binding: GmailBinding,
                       fingerprints: HmacFingerprintProvider) -> MailboxPage | None:
    """先验证当前用户映射，再检查持久 owner 和锁；失败不接触 Google。"""
    await SqlGmailWebAccess(sessions).role(binding.actor)
    if store.binding(binding.actor) != binding:
        raise PermissionDenied("邮箱连接已经更新")
    actor = MailboxActor(tenant_id=binding.actor.tenant_id, employee_id=binding.actor.employee_id)
    repository = SqlMailboxRepository(sessions)
    checkpoint = await repository.checkpoint(actor, binding.mailbox_id)
    if checkpoint.email != binding.email:
        raise PermissionDenied("邮箱绑定已失效")
    lock = PostgresAdvisoryLock(engine, derive_advisory_lock_key(actor.tenant_id, "mailbox:" + binding.mailbox_id))
    try:
        if not await lock.acquire():
            return None
        await SqlGmailWebAccess(sessions).role(binding.actor)
        reader = GmailMailboxReader(GmailWebMailboxHttpProvider(
            store.credentials_file(binding.operation_id)), binding.email)
        return await MailboxSync(repository, actor, binding.mailbox_id, reader, fingerprints, lock).once()
    finally:
        await lock.close()


async def run(profile_file: Path) -> int:
    """仅消费显式私有配置，信号只结束页间等待，不取消在途页提交。"""
    profile = PilotConfig.read(profile_file)
    if profile.gmail_web_settings_file is None:
        raise ValueError("网页邮箱未配置")
    settings = load_gmail_settings(profile.gmail_web_settings_file)
    store = GmailWebStore(settings.root)
    engine = create_engine_from(profile.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fingerprints = HmacFingerprintProvider("v1", profile.resolve("PILOT_FINGERPRINT").encode())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    next_attempt: dict[str, tuple[float, bool]] = {}
    paused: set[str] = set()
    repository = SqlMailboxRepository(sessions)
    try:
        await assert_database_schema_current(engine)
        await verify_runtime_database_scope(engine, profile.tenant_id)
        while not stop.is_set():
            bindings = store.bindings(profile.tenant_id)
            current_operations = {binding.operation_id for binding in bindings}
            paused.intersection_update(current_operations)
            next_attempt = {key: value for key, value in next_attempt.items() if key in current_operations}
            progressed = False
            for binding in bindings:
                if stop.is_set():
                    break
                key = binding.operation_id
                if key in paused:
                    continue
                actor = MailboxActor(tenant_id=binding.actor.tenant_id, employee_id=binding.actor.employee_id)
                try:
                    deadline, may_wake = next_attempt.get(key, (0, True))
                    if loop.time() < deadline and (
                        not may_wake or not (await repository.checkpoint(actor, binding.mailbox_id)).sync_requested
                    ):
                        continue
                    page = await sync_binding(engine, sessions, store, binding, fingerprints)
                    if page is None:
                        next_attempt[key] = (loop.time() + 15, False)
                    else:
                        progressed = page.phase != "synced" or progressed
                        next_attempt[key] = (loop.time() + (60 if page.phase == "synced" else 0), True)
                except PermissionDenied:
                    paused.add(key)
                except MailboxFailure as error:
                    await repository.failed(actor, binding.mailbox_id, error.code)
                    if error.code in {"authorization_required", "account_mismatch", "configuration_invalid"}:
                        paused.add(key)
                    else:
                        next_attempt[key] = (loop.time() + max(15, error.retry_after), False)
                # 数据库或未知内部错误由进程安全边界处理，不把异常原文或邮箱内容打印。
            if not progressed and not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="云端本人 Gmail 后台同步")
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.profile))
    except Exception:  # noqa: BLE001 - 进程边界不泄露凭证、邮箱正文和数据库详情
        print('{"status":"failed","reason":"mailbox_worker_unavailable"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
