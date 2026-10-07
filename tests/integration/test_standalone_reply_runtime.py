"""实际 standalone 入口使用原单副本调度器；构造与失锁均无外部调用。"""

import asyncio
import socket
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import build_phase1_dependencies
from apps.api.pilot import UnconfiguredModelClient, runtime_settings
from apps.composition_support.email_inbound import InboundMailbox, InboundRuntimePorts
from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
from apps.scheduler_worker.standalone import (
    _create_standalone_factory,
    create_standalone_factory,
)
from connectors.gmail.inbound_transport import GmailInboundApiTransport
from connectors.object_store.config import S3ObjectStoreSettings
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    CampaignCreateRequest,
    SequenceStepRequest,
    StepIntent,
)
from domains.sending_identity.schemas import DomainRole
from domains.sending_identity.service import (
    Actor as SenderActor,
)
from domains.sending_identity.service import (
    AuthenticationResult,
    IdentityRegisterRequest,
    SendingIdentityScope,
)
from domains.sending_identity.service import (
    ScopeLevel as SenderScopeLevel,
)
from infra.pilot.config import PILOT_GMAIL_MAILBOX_ALIAS, PilotGmailConfig
from infra.standalone.settings import StandaloneModelSettings
from shared.errors import ValidationError
from shared.schemas.identifiers import new_id
from tests.integration.test_pilot_persistence import initialized
from tests.integration.test_pilot_persistence import (
    owned_profiles as owned_profiles,  # noqa: PLC0414
)
from tests.unit.test_pilot_profile import make_config
from tests.unit.test_standalone_model_settings import settings


class NoNetworkGmail(GmailInboundApiTransport):
    """构造期、无绑定扫描和失锁分支都不应发出 Gmail 请求。"""

    async def send(self, **kwargs):
        pytest.fail("意外发送邮件")

    async def search(self, **kwargs):
        pytest.fail("意外查询已发邮件")

    async def get_profile_history_id(self, **kwargs):
        pytest.fail("无绑定时意外访问 Gmail")

    async def list_feedback_messages(self, **kwargs):
        pytest.fail("无绑定时意外访问 Gmail")

    async def list_feedback_history(self, **kwargs):
        pytest.fail("无绑定时意外访问 Gmail")

    async def get_inbound_message(self, **kwargs):
        pytest.fail("无绑定时意外读取正文")


class NoModel:
    def resolve(self, reference):
        pytest.fail("构造或无任务循环不得读取模型凭证")

    def provider(self):
        pytest.fail("构造或无任务循环不得创建模型 Provider")


def reply_settings(enabled=True, *, research=False):
    return StandaloneModelSettings.model_validate(
        {
            **settings(),
            "reply_enabled": enabled,
            "limits": {**settings()["limits"], "max_output_tokens": 512},
            **(
                {
                    "research": {
                        "secret_ref": "TEST_SEARCH_KEY",
                        "exclusive_account_confirmed": True,
                        "playbook_reader_user_id": "usr_test",
                        "maximum_artifact_bytes": 100000,
                        "search_timeout_seconds": 5,
                        "page_timeout_seconds": 5,
                    }
                }
                if research
                else {}
            ),
        }
    )


def gmail_profile(profile, tmp_path):
    return profile.model_copy(
        update={
            "gmail": PilotGmailConfig(
                address="synthetic@example.test",
                credentials_file=tmp_path / "missing-oauth.json",
                employee_id=new_id("emp"),
            )
        }
    )


def inbound_ports(profile):
    return InboundRuntimePorts(
        InboundMailbox(
            tenant_id=profile.tenant_id,
            mailbox_alias=PILOT_GMAIL_MAILBOX_ALIAS,
            route_id="pilot",
            config_version="pilot-gmail-v1",
        ),
        NoNetworkGmail(),
        profile,
        "GMAIL_OAUTH_TOKEN_REF",
        S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment()),
        "PILOT_FINGERPRINT",
        "reply-runtime-test",
    )


async def seed_draft(runtime, profile):
    """通过公开服务登记合成发件身份与初始草稿；不批准或启动 Campaign。"""
    sessions = async_sessionmaker(runtime.lock_engine, expire_on_commit=False)
    tenant, employee = runtime.tenant_id, new_id("emp")
    now = datetime.now(UTC)
    deps = build_phase1_dependencies(
        runtime_settings(profile),
        sessions,
        now=lambda: now,
        secret_resolver=profile,
        model_client=UnconfiguredModelClient(),
        gmail_transport=NoNetworkGmail(),
    )
    sender_boss = SenderActor(
        employee, SendingIdentityScope(level=SenderScopeLevel.TENANT), "boss"
    )
    try:
        sender = await deps.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                "synthetic@example.test", "example.test", DomainRole.COLD_OUTREACH
            ),
            actor=sender_boss,
        )
        await deps.sending_identities.begin_authentication(
            tenant, sender, actor=sender_boss
        )
        await deps.sending_identities.record_authentication_result(
            tenant,
            sender,
            AuthenticationResult(now, True, True, True, (), "controlled-draft"),
            actor=SenderActor(
                "system:draft-test",
                SendingIdentityScope(
                    level=SenderScopeLevel.SYSTEM,
                    allowed_identity_ids=frozenset({sender}),
                ),
                "system",
            ),
        )
        campaign = await deps.outreach.create_campaign(
            tenant,
            CampaignCreateRequest(
                name="未批准测试草稿",
                markets=("US",),
                target_entity_types=("importer",),
                allowed_categories=("hinges",),
                sender_identity_ids=(sender,),
                steps=(SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
                daily_new_contact_limit=1,
                daily_total_message_limit=1,
                handoff_triggers=(),
            ),
            actor=Actor(employee, OutreachScope(level=ScopeLevel.TENANT), "boss"),
        )
        return sessions, campaign.campaign_id
    finally:
        for lifecycle in (deps.model_lifecycle, deps.object_store_lifecycle):
            if lifecycle is not None:
                await lifecycle.aclose()


@pytest.mark.parametrize(
    "case",
    [
        "missing_gmail",
        "missing_employee",
        "disabled_ports",
        "disabled_gmail",
        "wrong_tenant",
        "wrong_mailbox",
        "wrong_route",
        "wrong_version",
    ],
)
async def test_standalone_reply_incomplete_or_cross_bound_configuration_refuses(
    tmp_path, case
):
    model = reply_settings(case not in {"disabled_ports", "disabled_gmail"})
    base = make_config(tmp_path).model_copy(update={"object_port": 19091})
    profile = base if case == "missing_gmail" else gmail_profile(base, tmp_path)
    ports = inbound_ports(profile) if case != "disabled_gmail" else None
    if case == "missing_employee":
        profile = profile.model_copy(
            update={"gmail": profile.gmail.model_copy(update={"employee_id": ""})}
        )
    patch = {
        "wrong_tenant": {"tenant_id": new_id("tn")},
        "wrong_mailbox": {"mailbox_alias": "other"},
        "wrong_route": {"route_id": "other"},
        "wrong_version": {"config_version": "other"},
    }.get(case)
    if patch:
        ports = replace(ports, profile=ports.profile.model_copy(update=patch))
    with pytest.raises((ValueError, ValidationError)):
        async with create_standalone_factory(
            profile, model, NoModel(), reply_inbound=ports
        )():
            pytest.fail("不完整回复接线不应启动")


def test_standalone_reply_construction_is_offline(tmp_path):
    profile = gmail_profile(
        make_config(tmp_path).model_copy(update={"object_port": 19091}), tmp_path
    )
    factory = _create_standalone_factory(
        profile,
        reply_settings(),
        NoModel(),
        provider_factory=NoModel().provider,
        instance_id="reply-offline",
    )
    assert factory is not None


@pytest.mark.parametrize(
    "reply_enabled,research_enabled", [(False, False), (True, False), (True, True)]
)
def test_standalone_composes_reply_under_existing_singleton(
    owned_profiles, reply_enabled, research_enabled
):
    model = reply_settings(reply_enabled, research=research_enabled)
    directory, profiles = owned_profiles
    owned = initialized(directory, profiles)
    profile = gmail_profile(owned.config, directory) if reply_enabled else owned.config
    ports = inbound_ports(profile) if reply_enabled else None
    factory = create_standalone_factory(
        profile,
        model,
        NoModel(),
        provider_factory=NoModel().provider,
        reply_inbound=ports,
    )

    async def exercise():
        if research_enabled:
            from apps.scheduler_worker.bootstrap import ResearchRuntimePorts
            from apps.scheduler_worker.standalone import UnboundResearchClient
            from connectors.object_store.s3 import S3ObjectBlobTransport
            from tests.integration.test_search_quota import (
                Pages,
                SearchTransport,
                Secrets,
            )

            owned_objects = S3ObjectBlobTransport(
                S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment()),
                profile,
            )
            search = SearchTransport(limit=100)
            research = ResearchRuntimePorts(
                UnboundResearchClient(),
                model.model,
                "usr_test",
                search,
                Pages(),
                owned_objects,
                100000,
                "TEST_SEARCH_KEY",
                Secrets(),
                True,
            )
            selected = create_standalone_factory(
                profile,
                model,
                NoModel(),
                provider_factory=NoModel().provider,
                reply_inbound=ports,
                research_ports=research,
            )
        else:
            selected = factory
        try:
            await exercise_runtime(selected)
        finally:
            if research_enabled:
                assert search.calls == 0
                await owned_objects.aclose()

    async def exercise_runtime(selected):
        async with selected() as runtime:
            sessions, campaign = await seed_draft(runtime, profile)
            assert (runtime.inbound_driver is not None) is reply_enabled
            assert runtime.assistant_driver is not None
            stop = asyncio.Event()

            async def one_cycle(interval, event):
                stop.set()

            async with runtime.lock_engine.connect() as blocker:
                key = runtime.config.lock_key
                await blocker.execute(
                    text("SELECT pg_advisory_lock(:key)"), {"key": key}
                )
                try:
                    failed = await run_scheduler_worker(
                        runtime,
                        stop_event=stop,
                        wait=one_cycle,
                        install_signal_handlers=False,
                    )
                    assert (
                        failed.status == WorkerStartStatus.NOT_STARTED
                        and failed.cycles_completed == 0
                    )
                finally:
                    await blocker.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                    )
            result = await run_scheduler_worker(
                runtime, stop_event=stop, wait=one_cycle, install_signal_handlers=False
            )
            assert (
                result.status == WorkerStartStatus.STARTED
                and result.cycles_completed == 1
            )
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT state FROM outreach_campaigns WHERE tenant_id=:t AND campaign_id=:c"
                        ),
                        {"t": runtime.tenant_id, "c": campaign},
                    )
                    == "draft"
                )

    asyncio.run(exercise())


def test_standalone_reply_failure_and_cancellation_release_resources(owned_profiles):
    directory, profiles = owned_profiles
    owned = initialized(directory, profiles)
    profile = gmail_profile(owned.config, directory)
    factory = create_standalone_factory(
        profile,
        reply_settings(),
        NoModel(),
        provider_factory=NoModel().provider,
        reply_inbound=inbound_ports(profile),
    )

    async def exercise():
        for failure in (ValueError, asyncio.CancelledError):
            with pytest.raises(failure):
                async with factory() as runtime:

                    async def fail_after_cycle(interval, event, error=failure):
                        raise error()

                    await run_scheduler_worker(
                        runtime,
                        stop_event=asyncio.Event(),
                        wait=fail_after_cycle,
                        install_signal_handlers=False,
                    )
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", profile.scheduler_port))
        async with factory() as runtime:
            stop = asyncio.Event()

            async def done(interval, event):
                stop.set()

            result = await run_scheduler_worker(
                runtime, stop_event=stop, wait=done, install_signal_handlers=False
            )
            assert (
                result.status == WorkerStartStatus.STARTED
                and result.cycles_completed == 1
            )

    asyncio.run(exercise())
