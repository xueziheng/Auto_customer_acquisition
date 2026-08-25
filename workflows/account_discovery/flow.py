"""account_discovery 持久状态机定义与 handler 装配。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from domains.employees.service import EmployeeService
from domains.outreach.service import OutreachService
from domains.prospecting.service import ProspectingService
from workflows.account_discovery.ports import (
    AccountDiscoveryActorResolver,
    AccountDiscoveryCapability,
    AccountDiscoveryTaskReader,
    ContactEnricher,
    ContactVerifier,
)
from workflows.account_discovery.steps import (
    AssignOwnerStep,
    AwaitCampaignActivationStep,
    BindCampaignStep,
    EnrollCampaignStep,
    FindCompanyDetailsStep,
    FindContactsStep,
    ResolveAccountStep,
    VerifyContactsStep,
)
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
    WorkflowEngine,
)

WORKFLOW_TYPE = "account_discovery"


def build_account_discovery_definition() -> WorkflowDefinition:
    """企业解析 → 联系人补全 → 验证 → 归属 → Campaign 入组。"""
    return WorkflowDefinition(
        workflow_type=WORKFLOW_TYPE,
        version=2,
        steps=(
            StepDefinition("bind_campaign", "account_discovery.bind_campaign"),
            StepDefinition(
                "find_company_details",
                "account_discovery.find_company_details",
                retry_backoff=timedelta(minutes=2),
            ),
            StepDefinition("resolve_account", "account_discovery.resolve_account"),
            StepDefinition(
                "find_contacts",
                "account_discovery.find_contacts",
                max_retries=1,
                retry_backoff=timedelta(minutes=5),
            ),
            StepDefinition(
                "verify_contacts",
                "account_discovery.verify_contacts",
                max_retries=1,
                retry_backoff=timedelta(minutes=5),
            ),
            StepDefinition("assign_owner", "account_discovery.assign_owner"),
            StepDefinition(
                "await_campaign_activation",
                "account_discovery.await_campaign_activation",
                wait_event_type="CampaignStateChanged",
                run_on_entry=True,
            ),
            StepDefinition("enroll_campaign", "account_discovery.enroll_campaign"),
        ),
        transitions={
            "bind_campaign": ("find_company_details",),
            "find_company_details": ("resolve_account",),
            "resolve_account": ("find_contacts",),
            "find_contacts": ("verify_contacts",),
            "verify_contacts": ("assign_owner",),
            "assign_owner": ("await_campaign_activation",),
            "await_campaign_activation": ("enroll_campaign",),
            "enroll_campaign": (),
        },
    )


def build_account_discovery_handlers(
    *,
    task_reader: AccountDiscoveryTaskReader,
    capability: AccountDiscoveryCapability,
    prospecting: ProspectingService,
    enricher: ContactEnricher,
    verifier: ContactVerifier,
    employees: EmployeeService,
    outreach: OutreachService,
    actor_resolver: AccountDiscoveryActorResolver,
    now: Callable[[], datetime],
) -> dict[str, StepHandler]:
    return {
        "account_discovery.bind_campaign": BindCampaignStep(
            outreach, actor_resolver
        ),
        "account_discovery.find_company_details": FindCompanyDetailsStep(
            task_reader, capability, actor_resolver
        ),
        "account_discovery.resolve_account": ResolveAccountStep(prospecting),
        "account_discovery.find_contacts": FindContactsStep(
            prospecting, enricher, now=now
        ),
        "account_discovery.verify_contacts": VerifyContactsStep(
            prospecting, verifier
        ),
        "account_discovery.assign_owner": AssignOwnerStep(
            employees, actor_resolver
        ),
        "account_discovery.await_campaign_activation": AwaitCampaignActivationStep(
            outreach, actor_resolver
        ),
        "account_discovery.enroll_campaign": EnrollCampaignStep(
            outreach, actor_resolver
        ),
    }


def register_account_discovery(engine: WorkflowEngine) -> None:
    engine.register(build_account_discovery_definition())


__all__ = (
    "WORKFLOW_TYPE",
    "build_account_discovery_definition",
    "build_account_discovery_handlers",
    "register_account_discovery",
)
