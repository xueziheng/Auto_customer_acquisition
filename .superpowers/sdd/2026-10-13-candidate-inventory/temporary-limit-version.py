"""一次性政策额度维护；UI创建版本，停机窗口只同步文件/恢复原head。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.prospect_inventory_tables import AcquisitionJobRow
from infra.db.session import create_engine_from
from infra.db.tables import (
    AgentTurnRow,
    ModelConfigurationHeadRow,
    ModelConfigurationVersionRow,
    ModelInvocationRow,
    ModelRuntimeProcessRow,
)
from infra.pilot.config import (
    PilotConfig,
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from infra.standalone.settings import StandaloneModelSettings, deployment_verification
from shared.schemas.identifiers import TenantId


class Journal(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )
    tenant: str
    actor: str
    settings_path: str
    old_version: str
    old_hash: str
    prepared_at: datetime
    new_version: str | None = None
    phase: Literal["prepared", "applying", "applied", "restoring", "restored"] = (
        "prepared"
    )


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Maintenance:
    """仅注入受信租户与私有路径；stdout不返回任何存储数据。"""

    def __init__(
        self, factory, tenant: str, actor: str, settings: Path, state: Path
    ) -> None:
        if (
            not tenant
            or not actor
            or any(not p.is_absolute() or ".." in p.parts for p in (settings, state))
        ):
            raise ValueError("maintenance_invalid")
        self.factory, self.tenant, self.actor = factory, tenant, actor
        self.settings, self.state = settings, state
        self.checkpoint = lambda stage: None

    def _directory(self) -> None:
        checked_directory(self.settings.parent)
        self.state.mkdir(mode=0o700, exist_ok=True)
        checked_directory(self.state)

    def _write(self, journal: Journal) -> None:
        private_write(self.state / "journal.json", journal.model_dump_json().encode())

    def _load(self) -> tuple[Journal, bytes, StandaloneModelSettings]:
        journal = Journal.model_validate_json(private_read(self.state / "journal.json"))
        old = private_read(self.state / "original.json")
        config = StandaloneModelSettings.model_validate_json(old)
        if (
            (
                journal.tenant,
                journal.actor,
                journal.settings_path,
                journal.old_hash,
                journal.old_version,
            )
            != (
                self.tenant,
                self.actor,
                str(self.settings),
                digest(old),
                config.configuration_version,
            )
            or config.limits.tenant_calls != 20
            or config.limits.employee_calls != 20
        ):
            raise ValueError("maintenance_invalid")
        return journal, old, config

    async def _head(self, db):
        return (
            await db.scalars(
                select(ModelConfigurationHeadRow)
                .where(ModelConfigurationHeadRow.tenant_id == self.tenant)
                .with_for_update()
            )
        ).one()

    async def _version(
        self,
        db,
        version: str,
        config: StandaloneModelSettings,
        *,
        new: Journal | None = None,
    ) -> None:
        row = (
            await db.scalars(
                select(ModelConfigurationVersionRow).where(
                    ModelConfigurationVersionRow.tenant_id == self.tenant,
                    ModelConfigurationVersionRow.version == version,
                )
            )
        ).one()
        if (
            row.model != config.model
            or row.limits != config.limits.model_dump()
            or row.export_enabled != config.model_data_export_enabled
        ):
            raise ValueError("maintenance_invalid")
        if new is not None and (
            not version.startswith("mcv_")
            or row.created_by != self.actor
            or row.created_at < new.prepared_at
        ):
            raise ValueError("maintenance_invalid")

    async def _quiet(self, db, config: StandaloneModelSettings, stopped: bool) -> None:
        if not stopped:
            raise ValueError("maintenance_requires_stopped_service")
        cutoff = datetime.now(UTC) - timedelta(
            seconds=max(90, config.limits.timeout_seconds * 2)
        )
        active = await db.scalar(
            select(ModelInvocationRow.invocation_id)
            .where(
                ModelInvocationRow.tenant_id == self.tenant,
                ModelInvocationRow.slot_released.is_(False),
            )
            .limit(1)
        )
        live = await db.scalar(
            select(ModelRuntimeProcessRow.instance_id)
            .where(
                ModelRuntimeProcessRow.tenant_id == self.tenant,
                ModelRuntimeProcessRow.heartbeat_at >= cutoff,
            )
            .limit(1)
        )
        work = await db.scalar(
            select(AcquisitionJobRow.job_id)
            .where(
                AcquisitionJobRow.tenant_id == self.tenant,
                AcquisitionJobRow.status.in_(("queued", "processing")),
            )
            .limit(1)
        )
        turns = await db.scalar(
            select(AgentTurnRow.turn_id)
            .where(
                AgentTurnRow.tenant_id == self.tenant,
                AgentTurnRow.state.in_(("queued", "running")),
            )
            .limit(1)
        )
        if (
            active is not None
            or live is not None
            or work is not None
            or turns is not None
        ):
            raise ValueError("maintenance_requires_quiescence")

    def _new_bytes(self, journal: Journal, old: bytes) -> bytes:
        if journal.new_version is None or journal.new_version == journal.old_version:
            raise ValueError("maintenance_invalid")
        data = json.loads(old)
        data["configuration_version"] = journal.new_version
        data["limits"]["tenant_calls"] = data["limits"]["employee_calls"] = 26
        data["api_verified_at"] = None
        result = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        StandaloneModelSettings.model_validate_json(result)
        return result

    async def prepare(self) -> None:
        self._directory()
        with (
            exclusive_profile_lock(self.settings.parent),
            exclusive_profile_lock(self.state),
        ):
            old = private_read(self.settings)
            config = StandaloneModelSettings.model_validate_json(old)
            if (
                config.limits.tenant_calls != 20
                or config.limits.employee_calls != 20
                or config.api_business_tenant != self.tenant
                or config.model_catalog_directory is None
            ):
                raise ValueError("maintenance_invalid")
            repo = SqlModelConfigurationRepository(
                self.factory,
                None,
                deployment_verification=deployment_verification(config),
            )
            snapshot = await repo.get(TenantId(self.tenant))
            if (
                snapshot is None
                or snapshot.runtime.verified_at is None
                or snapshot.runtime.failure_code is not None
            ):
                raise ValueError("maintenance_unverified")
            now = datetime.now(UTC)
            ttl = timedelta(seconds=max(90, config.limits.timeout_seconds * 2))
            if (
                snapshot.configuration_version,
                snapshot.runtime.api_version,
                snapshot.runtime.scheduler_version,
            ) != (config.configuration_version,) * 3 or any(
                t is None or not timedelta(0) <= now - t <= ttl
                for t in (
                    snapshot.runtime.api_heartbeat_at,
                    snapshot.runtime.scheduler_heartbeat_at,
                )
            ):
                raise ValueError("maintenance_invalid")
            async with self.factory() as db, db.begin():
                head = await self._head(db)
                if head.version != config.configuration_version:
                    raise ValueError("maintenance_conflict")
                await self._version(db, head.version, config)
                if (self.state / "journal.json").exists():
                    journal, backup, _ = self._load()
                    if journal.phase != "prepared" or backup != old:
                        raise ValueError("maintenance_conflict")
                    return
                if (self.state / "original.json").exists() and private_read(
                    self.state / "original.json"
                ) != old:
                    raise ValueError("maintenance_conflict")
                private_write(self.state / "original.json", old)
                self._write(
                    Journal(
                        tenant=self.tenant,
                        actor=self.actor,
                        settings_path=str(self.settings),
                        old_version=head.version,
                        old_hash=digest(old),
                        prepared_at=now,
                    )
                )

    async def _transition(
        self,
        action: Literal["apply", "restore"],
        new_version: str | None,
        stopped: bool,
    ) -> None:
        self._directory()
        with (
            exclusive_profile_lock(self.settings.parent),
            exclusive_profile_lock(self.state),
        ):
            journal, old, config = self._load()
            if new_version is not None:
                if journal.new_version not in (None, new_version):
                    raise ValueError("maintenance_conflict")
                journal = journal.model_copy(update={"new_version": new_version})
            if action == "apply" and journal.phase in ("restoring", "restored"):
                raise ValueError("maintenance_conflict")
            desired = self._new_bytes(journal, old) if journal.new_version else None
            current = private_read(self.settings)
            if current != old and current != desired:
                raise ValueError("maintenance_conflict")
            async with self.factory() as db, db.begin():
                await self._quiet(db, config, stopped)
                head = await self._head(db)
                await self._version(db, journal.old_version, config)
                if journal.new_version:
                    await self._version(
                        db,
                        journal.new_version,
                        StandaloneModelSettings.model_validate_json(desired),
                        new=journal,
                    )
                allowed = (
                    (journal.new_version,)
                    if action == "apply"
                    else (journal.old_version, journal.new_version)
                )
                if head.version not in allowed or (
                    action == "apply" and desired is None
                ):
                    raise ValueError("maintenance_conflict")
                journal = journal.model_copy(
                    update={"phase": "applying" if action == "apply" else "restoring"}
                )
                self._write(journal)
                if action == "apply":
                    private_write(self.settings, desired)
                    self.checkpoint("apply_file")
                else:
                    head.version = journal.old_version
            if action == "restore":
                self.checkpoint("restore_head")
                private_write(self.settings, old)
                self.checkpoint("restore_file")
            self._write(
                journal.model_copy(
                    update={"phase": "applied" if action == "apply" else "restored"}
                )
            )

    async def apply(self, new_version: str, *, stopped: bool) -> None:
        await self._transition("apply", new_version, stopped)

    async def restore(self, new_version: str | None, *, stopped: bool) -> None:
        await self._transition("restore", new_version, stopped)


async def run(args) -> None:
    profile = PilotConfig.read(args.profile)
    engine = create_engine_from(profile.runtime_environment()["DATABASE_URL"])
    try:
        tool = Maintenance(
            async_sessionmaker(engine, expire_on_commit=False),
            profile.tenant_id,
            args.actor_user_id,
            args.settings,
            args.state_dir,
        )
        if args.action == "prepare":
            await tool.prepare()
        elif args.action == "apply":
            if not args.new_version:
                raise ValueError("maintenance_invalid")
            await tool.apply(args.new_version, stopped=args.service_stopped_sole_writer)
        else:
            await tool.restore(
                args.new_version, stopped=args.service_stopped_sole_writer
            )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "apply", "restore"))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--actor-user-id", required=True)
    parser.add_argument("--new-version")
    parser.add_argument("--service-stopped-sole-writer", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except Exception:  # noqa: BLE001 -- 固定结果，不打印异常/参数/数据库或私有文件内容。
        print("maintenance_refused_or_incomplete", file=sys.stderr)
        return 2
    print("maintenance_completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
