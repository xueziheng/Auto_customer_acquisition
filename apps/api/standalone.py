"""独立本机 API：复用登录和静态 Web，模型由后台运行。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.gmail_web import GmailWebService
from apps.composition_support.email_inbound import InboundMailbox
from connectors.gmail.inbound_transport import GmailInboundApiTransport
from connectors.gmail.send_oauth import GmailOAuthSecretResolver, GmailOAuthTokenSource
from connectors.object_store.config import S3ObjectStoreSettings
from infra.authentication.account_creation import PostgresAccountCreation
from infra.authentication.service import PostgresAuthentication
from infra.browser_probe import ungm_probe_enabled
from infra.db.inventory_sources import InventorySources
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.platform_access import EnterpriseReaderBinding
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.search_quota import PostgresSearchQuotaRepository
from infra.db.session import create_engine_from
from infra.pilot.config import PILOT_GMAIL_MAILBOX_ALIAS, PilotConfig
from infra.standalone.automatic_followup import reply_employee_binding
from infra.standalone.gmail_settings import load_gmail_settings
from infra.standalone.hunter_settings import load_hunter_customer_settings
from infra.standalone.knowledge_settings import load_knowledge_settings
from infra.standalone.platform_settings import PlatformSettings, load_platform_settings
from infra.standalone.settings import (
    StandaloneModelSettings,
    deployment_verification,
    load_model_settings,
)
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from workflows.employee_accounts import EmployeeAccountCreation

from .composition.aliyun_web import AliyunOutboundConnectors, AliyunWebService
from .composition.assistant import build_api_assistant
from .composition.customer_assignment import CustomerAssignmentBridge
from .composition.employee_mail import EmployeeMailConnectors
from .composition.inventory import CurrentCustomerAgentAuthority, bind_inventory
from .composition.qq_web import QQWebService, qq_connector
from .composition.standalone_contacts import build_standalone_customer_verification
from .pilot import UnconfiguredModelClient, mount_web, runtime_settings
from .platform_access import create_platform_app
from .runtime import create_runtime_app_from_settings


def create_standalone_app(
    profile: PilotConfig, settings: StandaloneModelSettings, build: Path,
    *, platform_settings: PlatformSettings | None = None,
    data_sources_directory: Path | None = None,
    model_api_directory: Path | None = None,
    aliyun_mail_directory: Path | None = None,
    qq_mail_directory: Path | None = None,
    hunter_customer_settings_file: Path | None = None,
) -> FastAPI:
    reply_employee_binding(profile, settings)
    knowledge = load_knowledge_settings(profile.knowledge_settings_file) if profile.knowledge_settings_file is not None else None
    runtime = runtime_settings(profile)
    hunter_customer_settings = load_hunter_customer_settings(hunter_customer_settings_file)
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
    aliyun = AliyunWebService(aliyun_mail_directory, sessions, fingerprints) if aliyun_mail_directory is not None else None
    qq = QQWebService(qq_mail_directory, sessions, fingerprints) if qq_mail_directory is not None else None
    gmail_web = GmailWebService(load_gmail_settings(profile.gmail_web_settings_file), sessions, fingerprints) if profile.gmail_web_settings_file is not None else None
    from apps.composition_support.email_inbound import qq_adapter
    qq_reply = settings.qq_reply
    if qq_reply is not None and qq_reply.continuous is not None and qq_reply.continuous.route.tenant_id != profile.tenant_id:
        raise ValueError("持续收信不能跨企业")
    if qq_reply is not None and qq_mail_directory != qq_reply.mailbox_directory:
        raise ValueError("QQ入站必须绑定API当前私有邮箱目录")
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
        enterprise_knowledge_enabled=knowledge is not None and knowledge.enabled,
        knowledge_maximum_upload_bytes=knowledge.max_upload_bytes if knowledge is not None else 10 * 1024 * 1024,
        secret_resolver=secret_resolver,
        object_store_settings=S3ObjectStoreSettings.from_pilot_environ(
            profile.runtime_environment()
        ),
        model_client=UnconfiguredModelClient(),
        gmail_transport=GmailInboundApiTransport() if gmail is not None else None,
        outbound_mail_connectors=EmployeeMailConnectors(gmail_web,
            AliyunOutboundConnectors(aliyun.store, sessions) if aliyun is not None else None,
            AliyunOutboundConnectors(qq.store, sessions, connector_factory=qq_connector) if qq is not None else None,
        ) if gmail_web is not None or aliyun is not None or qq is not None else None,
        gmail_sender_address=gmail.address if gmail is not None else None,
        automatic_followup_enabled=settings.automatic_send_enabled,
        private_reply_tests_enabled=qq_reply is not None,
        inbound_adapter_factory=(
            lambda factory: qq_adapter(qq_reply, TenantId(profile.tenant_id), factory)
        ) if qq_reply is not None else None,
        inbound_mailbox=(
            InboundMailbox(
                tenant_id=qq_reply.continuous.route.tenant_id,
                mailbox_alias=qq_reply.continuous.route.mailbox_alias,
                route_id=qq_reply.continuous.route.route_id,
                config_version=qq_reply.continuous.route.config_version,
            ) if qq_reply is not None and qq_reply.continuous is not None else
            InboundMailbox(
                tenant_id=TenantId(profile.tenant_id), mailbox_alias="qq-project-reply",
                route_id="pilot", config_version="qq-private-test-v1",
            ) if qq_reply is not None else
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
        customer_verification_factory=partial(
            build_standalone_customer_verification,
            tenant_id=TenantId(profile.tenant_id),
            settings=hunter_customer_settings,
            fingerprints=fingerprints,
        ),
    )
    sessions.configure(bind=business.state.runtime_engine)
    if qq_reply is not None:
        from apps.api.composition.private_reply_test import PrivateReplyTestApplication
        from infra.db.private_reply_test import PrivateReplyTestStore
        business.state.private_reply_test = PrivateReplyTestApplication(
            qq_reply, TenantId(profile.tenant_id),
            PrivateReplyTestStore(sessions, now=lambda: datetime.now(UTC)),
            business.state.dependencies.outreach,
        )
    if aliyun is not None:
        business.state.aliyun_web = aliyun
    if qq is not None:
        business.state.qq_web = qq
        from domains.conversations.business_mail import BusinessMailService
        from domains.conversations.business_mail_followup import (
            BusinessMailFollowupService,
        )
        from infra.business_mail_binding import CurrentBusinessMailboxBinding
        from infra.db.business_mail import SqlBusinessMailRepository
        from infra.db.business_mail_followup import SqlBusinessMailFollowupRepository
        business_mail_repository = SqlBusinessMailRepository(
            sessions, CurrentBusinessMailboxBinding(qq.store))
        business.state.business_mail = BusinessMailService(business_mail_repository)
        business.state.business_mail_followup = BusinessMailFollowupService(
            SqlBusinessMailFollowupRepository(business_mail_repository))
    business.state.employee_accounts = EmployeeAccountCreation(
        TenantId(profile.tenant_id),
        PostgresAccountCreation(sessions, TenantId(profile.tenant_id)),
    )
    if gmail_web is not None:
        business.state.gmail_web = gmail_web
    platform = None
    if platform_settings is not None:
        if platform_settings.control_tenant_id == profile.tenant_id:
            raise ValueError("平台和企业租户必须独立")
        # 存储重启可重新映射端口；控制租户仍必须使用自己的独立数据库身份。
        current_platform = platform_settings.model_copy(
            update={"database_port": profile.database_port},
        )
        platform = create_platform_app(
            control_tenant=TenantId(current_platform.control_tenant_id),
            engine=create_engine_from(current_platform.database_url.get_secret_value()),
            origin=f"http://127.0.0.1:{profile.api_port}",
            readers=(EnterpriseReaderBinding(
                tenant_id=TenantId(profile.tenant_id), engine=business.state.runtime_engine,
            ),),
            data_sources_directory=data_sources_directory,
            model_api_directory=model_api_directory,
            model_catalog_settings=settings,
            data_sources_fingerprints=fingerprints,
            ungm_browser_probe=ungm_probe_enabled(data_sources_directory, TenantId(current_platform.control_tenant_id)),
        )
        catalog = PostgresPublicCatalog(
            async_sessionmaker(platform.state.runtime_engine, expire_on_commit=False),
            TenantId(current_platform.control_tenant_id),
        )
        inventory = PostgresInventoryService(sessions, TenantId(profile.tenant_id), catalog,
            assignment_bridge=CustomerAssignmentBridge(),
            followup_authority=CurrentCustomerAgentAuthority(business.state.dependencies, TenantId(profile.tenant_id)))
        business.state.prospect_inventory_factory = lambda identity: bind_inventory(
            inventory, business.state.dependencies, identity,
        )
        business.state.inventory_sources = InventorySources(
            tenant_id=TenantId(profile.tenant_id),
            backend=inventory,
            model_snapshot=SqlModelConfigurationRepository(
                sessions, fingerprints,
                deployment_verification=deployment_verification(settings),
                deployment_provider=settings.provider,
            ).get,
            quota=PostgresSearchQuotaRepository(
                sessions, TenantId(profile.tenant_id), now=lambda: datetime.now(UTC),
            ),
            search_policy=settings.inventory_search,
            configured=(settings.research is not None
                        and settings.research.exclusive_account_confirmed),
        )
        platform.state.inventory_companies = {profile.tenant_id: inventory}
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
            data_sources_directory=args.profile.absolute().parent / "data-sources",
            model_api_directory=args.model_settings.absolute().parent / "api-settings",
            aliyun_mail_directory=(args.profile.absolute().parent / "aliyun-mail"
                if (args.profile.absolute().parent / "aliyun-mail").is_dir() else None),
            qq_mail_directory=(args.profile.absolute().parent / "qq-mail"
                if (args.profile.absolute().parent / "qq-mail").is_dir() else None),
            hunter_customer_settings_file=(args.profile.absolute().parent / "hunter-customer-settings.json"
                if (args.profile.absolute().parent / "hunter-customer-settings.json").is_file() else None),
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
