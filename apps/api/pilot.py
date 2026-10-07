"""真实会话的本机内测入口；同源生产 Web 与唯一 canonical API。"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.composition_support.email_inbound import InboundMailbox
from connectors.gmail.inbound_transport import GmailInboundApiTransport
from connectors.gmail.send_oauth import GmailOAuthSecretResolver, GmailOAuthTokenSource
from connectors.object_store.config import S3ObjectStoreSettings
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from infra.authentication.service import PostgresAuthentication
from infra.controlled.network import (
    install_network_boundary,
    resolve_external_destinations,
)
from infra.pilot.config import PILOT_GMAIL_MAILBOX_ALIAS, PilotConfig, PilotError
from shared.schemas.identifiers import TenantId
from shared.schemas.money import CurrencyCode, Money
from workflows.email_feedback.unsubscribe import UnsubscribeKeyReference

from .runtime import create_runtime_app_from_settings
from .runtime_config import Phase1RuntimeSettings


class UnconfiguredModelClient:
    """未配置模型的拒绝端口，永不返回合成业务结果。"""

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        raise PilotError("model_not_configured")


def runtime_settings(config: PilotConfig) -> Phase1RuntimeSettings:
    """只映射已校验 profile；不借用要求 dev=true 的旧环境 parser。"""
    env = config.runtime_environment()
    handoff = config.policy.handoff_policy
    scoring = config.policy.scoring_policy
    return Phase1RuntimeSettings(
        database_url=config.database_url,
        tenant_id=config.tenant_id,
        dev_mode=False,
        cors_allowed_origins=(),
        retry_after_seconds=int(env["TRADEOS_API_RETRY_AFTER_SECONDS"]),
        handoff_policy=HandoffPolicy(handoff.sla_seconds, handoff.backlog_threshold),
        t1=timedelta(seconds=handoff.t1_seconds),
        t2=timedelta(seconds=handoff.t2_seconds),
        owner_reminder_interval=(
            timedelta(seconds=handoff.owner_reminder_interval_seconds)
            if handoff.owner_reminder_interval_seconds is not None
            else None
        ),
        scoring_policy=ScoringPolicy(
            scoring.version,
            tuple(
                Money(v, CurrencyCode(scoring.currency))
                for v in scoring.value_band_boundaries
            ),
            {int(k): v for k, v in scoring.bucket_map.items()},
        ),
        outbox_max_attempts=int(env["TRADEOS_OUTBOX_MAX_ATTEMPTS"]),
        gmail_oauth_token_ref=(
            "GMAIL_OAUTH_TOKEN_REF" if config.gmail is not None else ""
        ),
        openai_api_key_ref="",
        trade_manager_model="unconfigured",
        tool_call_fingerprint_key_ref=env["TOOL_CALL_FINGERPRINT_KEY_REF"],
        tool_call_fingerprint_key_version=env["TOOL_CALL_FINGERPRINT_KEY_VERSION"],
        unsubscribe_base_url=env["TRADEOS_UNSUBSCRIBE_BASE_URL"],
        email_feedback_route_id=env["TRADEOS_EMAIL_FEEDBACK_ROUTE_ID"],
        unsubscribe_active_key_id=env["TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID"],
        unsubscribe_key_refs=(UnsubscribeKeyReference("v1", "PILOT_UNSUBSCRIBE"),),
        tool_lease=timedelta(seconds=int(env["TRADEOS_TOOL_LEASE_SECONDS"])),
    )


def mount_web(business: FastAPI, build: Path) -> FastAPI:
    """显式进入被挂载 API lifespan；路径穿越、未知资源不回退 SPA。"""
    build = build.resolve()
    if not (build / "index.html").is_file():
        raise PilotError("web_build_missing")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with business.router.lifespan_context(business):
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/api", business)

    @app.get("/{path:path}", include_in_schema=False)
    async def web(request: Request, path: str) -> Response:
        if ".." in path.split("/") or "\\" in path or path == "api":
            return Response(status_code=404)
        target = (build / path).resolve()
        if not target.is_relative_to(build):
            return Response(status_code=404)
        if target.is_file():
            return FileResponse(target, headers={"Cache-Control": "no-cache"})
        if path.startswith("assets/") or "." in path.rsplit("/", 1)[-1]:
            return Response(status_code=404)
        return FileResponse(build / "index.html", headers={"Cache-Control": "no-store"})

    app.state.business_app = business
    return app


def create_pilot_app(config: PilotConfig, build: Path) -> FastAPI:
    """认证仓储绑定 canonical API 自有 engine，不新建数据库生命周期。"""
    factory = async_sessionmaker(expire_on_commit=False)
    authentication = PostgresAuthentication(factory, TenantId(config.tenant_id))
    gmail = config.gmail
    gmail_transport = GmailInboundApiTransport() if gmail is not None else None
    secret_resolver = (
        GmailOAuthSecretResolver(
            config, GmailOAuthTokenSource(gmail.credentials_file, gmail.address)
        )
        if gmail is not None
        else config
    )
    business = create_runtime_app_from_settings(
        runtime_settings(config),
        secret_resolver=secret_resolver,
        object_store_settings=S3ObjectStoreSettings.from_pilot_environ(
            config.runtime_environment()
        ),
        model_client=UnconfiguredModelClient(),
        gmail_transport=gmail_transport,
        inbound_mailbox=(
            InboundMailbox(
                tenant_id=TenantId(config.tenant_id),
                mailbox_alias=PILOT_GMAIL_MAILBOX_ALIAS,
                route_id="pilot",
                config_version="pilot-gmail-v1",
            )
            if gmail is not None
            else None
        ),
        authentication=authentication,
        authentication_origin=f"http://127.0.0.1:{config.api_port}",
    )
    factory.configure(bind=business.state.runtime_engine)
    return mount_web(business, build)


def main() -> int:
    """只消费本 profile 与继承监听 socket，异常不输出底层材料。"""
    logging.disable(logging.CRITICAL)
    try:
        config = PilotConfig.read(Path(sys.argv[1]))
        external_hosts = (
            frozenset({("gmail.googleapis.com", 443), ("oauth2.googleapis.com", 443)})
            if config.gmail is not None
            else frozenset()
        )
        install_network_boundary(
            destinations=frozenset({config.database_port, config.object_port}),
            listeners=frozenset({config.api_port}),
            external_hosts=external_hosts,
            external_destinations=resolve_external_destinations(external_hosts),
        )
        app = create_pilot_app(config, Path(__file__).resolve().parents[1] / "web/dist")
        with socket.socket(fileno=int(sys.argv[2])) as listener:
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=config.api_port,
                    access_log=False,
                    log_config=None,
                    timeout_graceful_shutdown=15,
                )
            )
            asyncio.run(server.serve(sockets=[listener]))
        return 0 if server.started else 2
    except BaseException:  # noqa: BLE001 进程边界固定失败
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
