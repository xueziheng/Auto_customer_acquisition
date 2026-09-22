"""独立本机 API：复用登录和静态 Web，模型由后台运行。"""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace
from functools import partial
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.object_store.config import S3ObjectStoreSettings
from infra.authentication.service import PostgresAuthentication
from infra.pilot.config import PilotConfig
from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider

from .composition.assistant import build_api_assistant
from .pilot import UnconfiguredModelClient, mount_web, runtime_settings
from .runtime import create_runtime_app_from_settings


def create_standalone_app(
    profile: PilotConfig, settings: StandaloneModelSettings, build: Path
) -> FastAPI:
    runtime = runtime_settings(profile)
    if settings.research is not None:
        runtime = replace(runtime, tavily_api_key_ref=settings.research.secret_ref,
                          tavily_exclusive_account_confirmed=settings.research.exclusive_account_confirmed)
    fingerprints = HmacFingerprintProvider(
        runtime.tool_call_fingerprint_key_version,
        profile.resolve(runtime.tool_call_fingerprint_key_ref).encode(),
    )
    sessions = async_sessionmaker(expire_on_commit=False)
    business = create_runtime_app_from_settings(
        runtime,
        secret_resolver=profile,
        object_store_settings=S3ObjectStoreSettings.from_pilot_environ(
            profile.runtime_environment()
        ),
        model_client=UnconfiguredModelClient(),
        authentication=PostgresAuthentication(sessions, TenantId(profile.tenant_id)),
        authentication_origin=f"http://127.0.0.1:{profile.api_port}",
        assistant_factory=partial(
            build_api_assistant,
            tenant_id=TenantId(profile.tenant_id),
            settings=settings,
            fingerprints=fingerprints,
        ),
    )
    sessions.configure(bind=business.state.runtime_engine)
    return mount_web(business, build)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动独立本机 TradeOS API")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--model-settings", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        profile = PilotConfig.read(args.profile)
        model = load_model_settings(args.model_settings)
        app = create_standalone_app(
            profile, model, Path(__file__).resolve().parents[1] / "web/dist"
        )
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=profile.api_port,
            access_log=False,
            log_config=None,
        )
        return 0
    except BaseException:  # noqa: BLE001 - 进程边界只返回固定安全失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
