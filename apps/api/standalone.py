"""独立本机 API：复用登录和静态 Web，模型由后台运行。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from dataclasses import replace
from functools import partial
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.composition_support.email_inbound import InboundMailbox
from connectors.gmail.inbound_transport import GmailInboundApiTransport
from connectors.gmail.send_oauth import GmailOAuthSecretResolver, GmailOAuthTokenSource
from connectors.object_store.config import S3ObjectStoreSettings
from infra.authentication.service import PostgresAuthentication
from infra.db.platform_access import EnterpriseReaderBinding
from infra.db.session import create_engine_from
from infra.pilot.config import PILOT_GMAIL_MAILBOX_ALIAS, PilotConfig
from infra.standalone.platform_settings import PlatformSettings, load_platform_settings
from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider

from .composition.assistant import build_api_assistant
from .pilot import UnconfiguredModelClient, mount_web, runtime_settings
from .platform_access import create_platform_app
from .runtime import create_runtime_app_from_settings


def create_standalone_app(
    profile: PilotConfig, settings: StandaloneModelSettings, build: Path,
    *, platform_settings: PlatformSettings | None = None,
) -> FastAPI:
    runtime = runtime_settings(profile)
    if settings.research is not None:
        runtime = replace(
            runtime,
            tavily_api_key_ref=settings.research.secret_ref,
            tavily_exclusive_account_confirmed=settings.research.exclusive_account_confirmed,
        )
    fingerprints = HmacFingerprintProvider(
        runtime.tool_call_fingerprint_key_version,
        profile.resolve(runtime.tool_call_fingerprint_key_ref).encode(),
    )
    sessions = async_sessionmaker(expire_on_commit=False)
    gmail = profile.gmail
    secret_resolver = (
        GmailOAuthSecretResolver(
            profile, GmailOAuthTokenSource(gmail.credentials_file, gmail.address)
        )
        if gmail is not None
        else profile
    )
    business = create_runtime_app_from_settings(
        runtime,
        secret_resolver=secret_resolver,
        object_store_settings=S3ObjectStoreSettings.from_pilot_environ(
            profile.runtime_environment()
        ),
        model_client=UnconfiguredModelClient(),
        gmail_transport=GmailInboundApiTransport() if gmail is not None else None,
        inbound_mailbox=(
            InboundMailbox(
                tenant_id=TenantId(profile.tenant_id),
                mailbox_alias=PILOT_GMAIL_MAILBOX_ALIAS,
                route_id="pilot",
                config_version="pilot-gmail-v1",
            )
            if gmail is not None
            else None
        ),
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
    platform = None
    if platform_settings is not None:
        if platform_settings.control_tenant_id == profile.tenant_id:
            raise ValueError("平台和企业租户必须独立")
        platform = create_platform_app(
            control_tenant=TenantId(platform_settings.control_tenant_id),
            engine=create_engine_from(platform_settings.database_url.get_secret_value()),
            origin=f"http://127.0.0.1:{profile.api_port}",
            readers=(EnterpriseReaderBinding(
                tenant_id=TenantId(profile.tenant_id), engine=business.state.runtime_engine,
            ),),
        )
    return mount_web(business, build, platform=platform)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动独立本机 TradeOS API")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--model-settings", type=Path, required=True)
    parser.add_argument("--listen-fd", type=int)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        profile = PilotConfig.read(args.profile)
        model = load_model_settings(args.model_settings)
        app = create_standalone_app(
            profile, model, Path(__file__).resolve().parents[1] / "web/dist",
            platform_settings=(load_platform_settings(profile.platform_settings_file)
                               if profile.platform_settings_file is not None else None),
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=profile.api_port,
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=15,
        )
        server = uvicorn.Server(config)
        if args.listen_fd is None:
            asyncio.run(server.serve())
        else:
            with socket.socket(fileno=args.listen_fd) as listener:
                if listener.getsockname()[:2] != ("127.0.0.1", profile.api_port):
                    return 2
                asyncio.run(server.serve(sockets=[listener]))
        return 0 if server.started else 2
    except BaseException:  # noqa: BLE001 - 进程边界只返回固定安全失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
