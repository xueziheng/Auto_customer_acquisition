"""本机老板显式授权的本人邮箱测试；复用生产 Gateway 账本与 Gmail 连接器。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.gmail.client import GmailConnector
from connectors.gmail.send_oauth import GmailOAuthSecretResolver, GmailOAuthTokenSource
from connectors.gmail.transport import GmailApiHttpTransport
from infra.controlled.network import (
    install_network_boundary,
    resolve_external_destinations,
)
from infra.db.session import create_engine_from
from infra.db.tables import (
    ContactPointRow,
    EmployeeRow,
    OutreachSuppressionRow,
    ProspectContactRow,
    SendingIdentityRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import (
    PilotConfig,
    PilotError,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.mailbox_test import (
    MANIFEST,
    STAGES,
    MailTestCheck,
    MailTestGrant,
    MailTestHandler,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolCallContext, ToolGateway


def encode(grant: MailTestGrant) -> bytes:
    value = asdict(grant)
    value["approved_at"] = grant.approved_at.isoformat()
    value["expires_at"] = grant.expires_at.isoformat()
    return json.dumps(value, sort_keys=True).encode()


def read_grant(path: Path) -> MailTestGrant:
    value = json.loads(private_read(path))
    value["approved_at"] = datetime.fromisoformat(value["approved_at"])
    value["expires_at"] = datetime.fromisoformat(value["expires_at"])
    return MailTestGrant(**value)


class CurrentPolicy:
    """读取当前租户真实员工、身份限制及已有抑制；不制造客户或认证事实。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config_path: Path,
        grant_path: Path,
    ) -> None:
        self.sessions, self.config_path, self.grant_path = (
            sessions,
            config_path,
            grant_path,
        )

    async def check(self, stage: str, grant: MailTestGrant) -> bool:
        config = PilotConfig.read(self.config_path)
        if (
            read_grant(self.grant_path) != grant
            or config.tenant_id != grant.tenant_id
            or config.gmail is None
            or config.gmail.address != grant.sender
            or config.gmail.employee_id != grant.employee_id
        ):
            return False
        async with self.sessions() as session:
            owner = (
                await session.execute(
                    select(EmployeeRow.role, EmployeeRow.is_active).where(
                        EmployeeRow.tenant_id == grant.tenant_id,
                        EmployeeRow.employee_id == grant.employee_id,
                    )
                )
            ).one_or_none()
            if owner is None or owner.role != "boss" or owner.is_active is not True:
                return False
            states = (
                (
                    await session.execute(
                        select(SendingIdentityRow.state).where(
                            SendingIdentityRow.tenant_id == grant.tenant_id,
                            SendingIdentityRow.address == grant.sender,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not states or any(
                value not in {"created", "auth_pending", "warming", "active"}
                for value in states
            ):
                return False
            if stage == "suppression" or stage == "rate_limit":
                count = await session.scalar(
                    select(func.count())
                    .select_from(ContactPointRow)
                    .join(
                        ProspectContactRow,
                        (ProspectContactRow.tenant_id == ContactPointRow.tenant_id)
                        & (ProspectContactRow.contact_id == ContactPointRow.contact_id),
                    )
                    .join(
                        OutreachSuppressionRow,
                        (OutreachSuppressionRow.tenant_id == ContactPointRow.tenant_id)
                        & or_(
                            OutreachSuppressionRow.contact_point_id
                            == ContactPointRow.contact_point_id,
                            OutreachSuppressionRow.account_id
                            == ProspectContactRow.account_id,
                        ),
                    )
                    .where(
                        ContactPointRow.tenant_id == grant.tenant_id,
                        ProspectContactRow.tenant_id == grant.tenant_id,
                        OutreachSuppressionRow.tenant_id == grant.tenant_id,
                        ContactPointRow.kind == "email",
                        func.lower(ContactPointRow.value) == grant.recipient.casefold(),
                    )
                )
                if count != 0:
                    return False
        return True


async def run(args: argparse.Namespace) -> int:
    config_path = args.profile / "config.json"
    config = PilotConfig.read(config_path)
    if config.gmail is None:
        raise PilotError("gmail_not_configured")
    engine = create_engine_from(config.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.action == "authorize":
            if not args.confirm_my_mailbox or not args.recipient:
                raise PilotError("explicit_recipient_confirmation_required")
            now = datetime.now(UTC)
            grant = MailTestGrant(
                new_id("mtg"),
                TenantId(config.tenant_id),
                config.gmail.employee_id,
                config.gmail.address,
                args.recipient.strip().casefold(),
                now,
                now + timedelta(minutes=45),
            )
            path = args.profile / (grant.grant_id + ".json")
            if path.exists():
                raise PilotError("grant_already_exists")
            private_write(path, encode(grant))
            policy = CurrentPolicy(sessions, config_path, path)
            if not await policy.check("approval", grant):
                path.unlink()
                raise PilotError("current_operator_or_identity_rejected")
            print(json.dumps({"status": "authorized", "grant_id": grant.grant_id}))
            return 0
        import re

        if not args.grant_id or not re.fullmatch(
            r"mtg_[0-7][0-9A-HJKMNP-TV-Z]{25}", args.grant_id
        ):
            raise PilotError("grant_required")
        path = args.profile / (args.grant_id + ".json")
        grant = read_grant(path)
        hosts = frozenset(
            {("gmail.googleapis.com", 443), ("oauth2.googleapis.com", 443)}
        )
        install_network_boundary(
            destinations=frozenset({config.database_port}),
            listeners=frozenset(),
            external_hosts=hosts,
            external_destinations=resolve_external_destinations(hosts),
        )
        transport = GmailApiHttpTransport(timeout_seconds=30)
        gmail = GmailConnector(transport)
        secrets = GmailOAuthSecretResolver(
            config,
            GmailOAuthTokenSource(config.gmail.credentials_file, config.gmail.address),
        )
        fingerprints = HmacFingerprintProvider(
            "v1", config.resolve("PILOT_FINGERPRINT").encode()
        )
        handler = MailTestHandler(grant, fingerprints, gmail, secrets)
        registry = ToolRegistry()
        registry.register(MANIFEST, handler)
        policy = CurrentPolicy(sessions, config_path, path)
        gateway = ToolGateway(
            registry,
            {name: MailTestCheck(name, grant, policy) for name in STAGES},
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(sessions, tenant),
            lease_duration=timedelta(seconds=120),
            lease_owner="operator_mailbox_test",
            id_factory=new_id,
        )
        result = await gateway.invoke(
            ToolCallContext(
                tenant_id=grant.tenant_id,
                user_id=UserId(grant.employee_id),
                tool_id=MANIFEST.tool_id,
                params={"grant_id": grant.grant_id},
                idempotency_key=grant.key,
                approval_ref=grant.grant_id,
            )
        )
        print(
            json.dumps(
                {
                    "status": result.status.value,
                    "tool_call_id": result.tool_call_id,
                    "error_category": result.error_category.value
                    if result.error_category
                    else None,
                    "rejection_stage": result.rejected.stage
                    if result.rejected
                    else None,
                    "provider_ref": result.output.get("provider_ref")
                    if result.output
                    else None,
                }
            )
        )
        return 0 if result.status.value in {"succeeded", "duplicate"} else 1
    finally:
        await engine.dispose()


def main() -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(
        description="仅向操作者本人指定邮箱发送固定测试邮件"
    )
    parser.add_argument("action", choices=["authorize", "send"])
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--recipient")
    parser.add_argument("--confirm-my-mailbox", action="store_true")
    parser.add_argument("--grant-id")
    args = parser.parse_args()
    try:
        with exclusive_profile_lock(args.profile):
            return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 固定进程错误，禁止泄露异常原文
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
