"""部署服务消费平台模型启用请求；不输出凭证，不启动客户任务。"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import (
    ModelConfigurationHeadRow,
    ModelConfigurationVersionRow,
    ModelRuntimeProcessRow,
)
from infra.model_api import FileModelApiRepository
from infra.pilot.config import (
    PilotConfig,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from infra.standalone.platform_settings import load_platform_settings
from infra.standalone.settings import load_model_settings


async def run(profile_path: Path, settings_path: Path, verify: bool) -> None:
    profile = PilotConfig.read(profile_path)
    platform = load_platform_settings(profile.platform_settings_file)
    store = FileModelApiRepository(settings_path.parent / "api-settings", platform.control_tenant_id)
    view = store.view(platform.control_tenant_id)
    if view.apply_status != "pending" or view.test_status != "passed":
        raise ValueError("模型启用请求无效")
    engine = create_engine_from(profile.runtime_environment()["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if verify:
            async with factory() as db:
                processes = (await db.scalars(select(ModelRuntimeProcessRow).where(ModelRuntimeProcessRow.tenant_id == profile.tenant_id))).all()
                head = (await db.scalars(select(ModelConfigurationHeadRow).where(ModelConfigurationHeadRow.tenant_id == profile.tenant_id))).one()
            ready = head.version == view.configuration_version and {p.process for p in processes} == {"api", "scheduler"} and all(p.version == view.configuration_version and datetime.now(UTC) - p.heartbeat_at < timedelta(seconds=90) for p in processes)
            store.mark_applied(platform.control_tenant_id, view.revision, ready)
            if not ready:
                raise ValueError("进程尚未就绪")
            return
        request = json.loads(private_read(store.directory / "apply-request.json"))
        if request != {"tenant_id": platform.control_tenant_id, "revision": view.revision}:
            raise ValueError("模型启用请求已变化")
        old_bytes = private_read(settings_path)
        old = load_model_settings(settings_path)
        updated = json.loads(old_bytes)
        updated.update(provider=view.provider, model=view.model, configuration_version=view.configuration_version,
            api_base_url=view.base_url, api_protocol=view.protocol,
            api_key_file=str(store.key_path(view.configuration_version)), api_control_tenant=platform.control_tenant_id,
            api_business_tenant=profile.tenant_id, api_verified_at=view.tested_at.isoformat())
        # 先验证整体配置；limits、外发许可和回复开关完全保留。
        type(old).model_validate(updated)
        with exclusive_profile_lock(settings_path.parent):
            async with factory() as db, db.begin():
                head = (await db.scalars(select(ModelConfigurationHeadRow).where(ModelConfigurationHeadRow.tenant_id == profile.tenant_id).with_for_update())).one()
                if head.version not in (view.active_version, view.configuration_version):
                    raise ValueError("企业模型版本已变更")
                row = (await db.scalars(select(ModelConfigurationVersionRow).where(ModelConfigurationVersionRow.tenant_id == profile.tenant_id, ModelConfigurationVersionRow.version == view.configuration_version))).one_or_none()
                if row is None:
                    db.add(ModelConfigurationVersionRow(tenant_id=profile.tenant_id, version=view.configuration_version,
                        model=view.model, limits=old.limits.model_dump(), export_enabled=old.model_data_export_enabled,
                        created_by="system:platform-model-api", created_at=datetime.now(UTC)))
                    await db.flush()
                elif row.model != view.model or row.limits != old.limits.model_dump() or row.export_enabled != old.model_data_export_enabled:
                    raise ValueError("配置版本冲突")
                head.version = view.configuration_version
                private_write(settings_path, json.dumps(updated).encode())
        (store.directory / "apply-request.json").unlink(missing_ok=True)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args.profile, args.settings, args.verify))
        print("模型配置已核验" if args.verify else "模型配置已应用，等待进程重载")
        return 0
    except Exception:  # noqa: BLE001 -- 只返回固定安全错误，禁止凭证和 SDK 原文泄漏
        try:
            profile = PilotConfig.read(args.profile)
            platform = load_platform_settings(profile.platform_settings_file)
            store = FileModelApiRepository(args.settings.parent / "api-settings", platform.control_tenant_id)
            view = store.view(platform.control_tenant_id)
            store.mark_applied(platform.control_tenant_id, view.revision, False)
            (store.directory / "apply-request.json").unlink(missing_ok=True)
        except Exception:  # noqa: BLE001, S110 -- 只输出外层固定失败，不泄露私有异常
            pass
        print("模型配置启用失败，请检查部署状态", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
