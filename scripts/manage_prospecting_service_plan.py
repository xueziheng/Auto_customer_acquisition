"""运维专用服务档位管理；默认预览，数据库凭证仅在显式执行时读取。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Never, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.service import (
    ProspectingServicePlanStore,
    ServicePlanState,
    ServiceTier,
    tier_policy,
)
from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore
from infra.db.schema import DatabaseSchemaError, assert_database_schema_current
from infra.db.session import create_engine_from
from shared.errors import (
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import TenantId

RuntimeFactory = Callable[[], AbstractAsyncContextManager[ProspectingServicePlanStore]]
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")


class _ArgumentParser(argparse.ArgumentParser):
    """参数错误不回显 argv，避免误放的凭证进入日志。"""

    def error(self, message: str) -> Never:
        del message
        raise ValueError("invalid_arguments")


class _DatabaseNotConfigured(RuntimeError):
    """只允许输出固定分类，不携带环境内容。"""


@dataclass(frozen=True)
class _Command:
    action: Literal["status", "assign"]
    tenant_id: TenantId
    actor_id: str | None = None
    tier: ServiceTier | None = None
    expected_version: int | None = None
    apply: bool = False


def _identifier(value: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not _SAFE_IDENTIFIER.fullmatch(value)
    ):
        raise ValueError("invalid_arguments")
    return value


def _parse(argv: Sequence[str] | None) -> _Command:
    parser = _ArgumentParser(
        prog="manage_prospecting_service_plan",
        allow_abbrev=False,
        description="运维读取/分配服务档位；assign 默认只预览，不开放普通用户升级。",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    status = commands.add_parser("status", allow_abbrev=False, help="读取安全状态")
    status.add_argument("--tenant-id", required=True)
    assign = commands.add_parser(
        "assign", allow_abbrev=False, help="后台分配档位或人工续期"
    )
    assign.add_argument("--tenant-id", required=True)
    assign.add_argument("--actor-id", required=True)
    assign.add_argument(
        "--tier", required=True, choices=("basic", "standard", "advanced")
    )
    assign.add_argument("--expected-version", type=int)
    mode = assign.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    tenant_id = TenantId(_identifier(args.tenant_id, 40))
    if args.action == "status":
        return _Command("status", tenant_id)
    actor_id = _identifier(args.actor_id, 64)
    if args.expected_version is not None and args.expected_version < 1:
        raise ValueError("invalid_arguments")
    return _Command(
        "assign",
        tenant_id,
        actor_id,
        cast(ServiceTier, args.tier),
        args.expected_version,
        args.apply,
    )


@asynccontextmanager
async def _database_runtime() -> AsyncIterator[ProspectingServicePlanStore]:
    """执行时从既有环境取 DSN；schema 不一致则不构造可写 Store。"""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise _DatabaseNotConfigured()
    engine = create_engine_from(database_url)
    try:
        await assert_database_schema_current(engine)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        yield PostgresProspectingServicePlanStore(factory)
    finally:
        await engine.dispose()


def _plan_projection(
    state: ServicePlanState | None,
    tenant_id: TenantId,
    now: datetime,
) -> dict[str, object] | None:
    """只投影已验证的计划字段；不输出提案、操作者、原文或整个存储对象。"""
    if state is None:
        return None
    state = ServicePlanState.model_validate(state.model_dump())
    if state.tenant_id != tenant_id:
        raise TenantIsolationViolation("service_plan_tenant_mismatch")
    return {
        "tier": state.tier,
        "version": state.version,
        "period_started_at": state.period_started_at.astimezone(UTC).isoformat(),
        "period_ends_at": state.period_ends_at.astimezone(UTC).isoformat(),
        "period_active": state.period_started_at <= now < state.period_ends_at,
        "candidates_reserved": state.candidates_reserved,
        "searches_reserved": state.searches_reserved,
        "contacts_reserved": state.contacts_reserved,
        "schedule_enabled": state.schedule_enabled,
        "next_run_at": state.next_run_at.astimezone(UTC).isoformat()
        if state.next_run_at
        else None,
        "last_run_at": state.last_run_at.astimezone(UTC).isoformat()
        if state.last_run_at
        else None,
    }


async def _execute(
    command: _Command,
    runtime_factory: RuntimeFactory,
    now: datetime,
) -> dict[str, object]:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock_invalid")
    now = now.astimezone(UTC)
    async with runtime_factory() as store:
        if command.action == "status":
            state = await store.get(command.tenant_id)
            result_status = "ok"
        else:
            if not command.apply or command.tier is None or command.actor_id is None:
                raise ValueError("apply_required")
            state = await store.assign(
                command.tenant_id,
                command.tier,
                expected_version=command.expected_version,
                actor_id=command.actor_id,
                now=now,
            )
            result_status = "applied"
        projection = _plan_projection(state, command.tenant_id, now)
    return {
        "status": result_status,
        "tenant_id": str(command.tenant_id),
        "plan": projection,
    }


def _emit(payload: dict[str, object], *, error: bool = False) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=sys.stderr if error else sys.stdout,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """默认 assign 仅预览；status/apply 才读数据库，所有错误输出固定分类。"""
    try:
        command = _parse(argv)
    except ValueError:
        _emit({"status": "not_run", "reason": "invalid_arguments"}, error=True)
        return 2
    if command.action == "assign" and not command.apply:
        assert command.tier is not None
        _emit(
            {
                "status": "dry_run",
                "tenant_id": str(command.tenant_id),
                "tier": command.tier,
                "expected_version": command.expected_version,
                "policy": tier_policy(command.tier).model_dump(mode="json"),
            }
        )
        return 0
    try:
        result = asyncio.run(
            _execute(
                command,
                runtime_factory or _database_runtime,
                now() if now else datetime.now(UTC),
            )
        )
    except _DatabaseNotConfigured:
        _emit({"status": "not_run", "reason": "database_not_configured"}, error=True)
        return 2
    except DatabaseSchemaError:
        _emit({"status": "not_run", "reason": "schema_not_current"}, error=True)
        return 3
    except (InvalidStateTransition, ValidationError):
        _emit({"status": "not_run", "reason": "request_rejected"}, error=True)
        return 3
    except Exception:  # noqa: BLE001 - DSN/驱动/提交回执异常不得进入终端或日志
        _emit({"status": "unknown", "reason": "execution_status_unknown"}, error=True)
        return 3
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
