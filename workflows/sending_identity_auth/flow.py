"""DNS 认证请求的单步、可恢复编排。"""

from __future__ import annotations

import re
from typing import Any, Protocol

from domains.sending_identity.service import (
    Actor,
    AuthCheck,
    AuthenticationCheckRequestStatus,
    AuthenticationFailure,
    AuthenticationFailureCategory,
    AuthenticationFixInstruction,
    AuthenticationResult,
    ScopeLevel,
    SendingIdentityScope,
    SendingIdentityService,
)
from shared.errors import ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import AuthenticationCheckRequested, DomainEvent
from shared.schemas.dns_auth import (
    DnsAuthenticationFacts,
    DnsAuthenticationFailureCategory,
)
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    RunId,
    SendingIdentityId,
    TenantId,
)
from workflows.engine.runner import (
    StepDefinition,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowRun,
)

WORKFLOW_TYPE = "sending_identity_authentication"
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_REQUEST = re.compile(rf"acr_{_ULID}\Z")
_IDENTITY = re.compile(rf"sid_{_ULID}\Z")


class DnsAuthenticationTool(Protocol):
    async def check(
        self,
        *,
        tenant_id: TenantId,
        run_id: RunId,
        request_id: AuthenticationCheckRequestId,
        sending_identity_id: SendingIdentityId,
        domain: str,
        dkim_selector: str,
    ) -> DnsAuthenticationFacts: ...


class OutboxHandlerRegistry(Protocol):
    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None: ...


def _system_actor(identity_id: SendingIdentityId) -> Actor:
    return Actor(
        "system_dns_auth",
        SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _context(
    run: WorkflowRun,
) -> tuple[AuthenticationCheckRequestId, SendingIdentityId]:
    if set(run.context) != {"request_id", "sending_identity_id"}:
        raise ValidationError("认证 workflow context 无效")
    request_id = run.context.get("request_id")
    identity_id = run.context.get("sending_identity_id")
    if (
        not isinstance(request_id, str)
        or _REQUEST.fullmatch(request_id) is None
        or request_id != run.subject_ref
        or not isinstance(identity_id, str)
        or _IDENTITY.fullmatch(identity_id) is None
    ):
        raise ValidationError("认证 workflow context 无效")
    return AuthenticationCheckRequestId(request_id), SendingIdentityId(identity_id)


def _domain_result(facts: DnsAuthenticationFacts) -> AuthenticationResult:
    category = {
        DnsAuthenticationFailureCategory.MISSING: (
            AuthenticationFailureCategory.RECORD_MISSING
        ),
        DnsAuthenticationFailureCategory.MALFORMED: (
            AuthenticationFailureCategory.RECORD_INVALID
        ),
        DnsAuthenticationFailureCategory.POLICY_UNSAFE: (
            AuthenticationFailureCategory.POLICY_INSUFFICIENT
        ),
    }
    checks = {"spf": AuthCheck.SPF, "dkim": AuthCheck.DKIM, "dmarc": AuthCheck.DMARC}
    instructions = {
        "spf": AuthenticationFixInstruction.CONFIGURE_SPF,
        "dkim": AuthenticationFixInstruction.CONFIGURE_DKIM,
        "dmarc": AuthenticationFixInstruction.CONFIGURE_DMARC,
    }
    return AuthenticationResult(
        facts.checked_at,
        facts.spf_passed,
        facts.dkim_passed,
        facts.dmarc_passed,
        tuple(
            AuthenticationFailure(
                checks[item.check], category[item.category], instructions[item.check]
            )
            for item in facts.failures
        ),
        f"dns_{facts.check_ref[:60]}",
    )


class DnsAuthenticationStep:
    """只消费公共发件服务和 Gateway checker，不接触 DNS 原文。"""

    def __init__(
        self,
        sending_identities: SendingIdentityService,
        tool: DnsAuthenticationTool,
        *,
        dkim_selector: str,
    ) -> None:
        from shared.schemas.dns_auth import DnsAuthenticationRequest

        DnsAuthenticationRequest("validation.example", dkim_selector)
        self._sending = sending_identities
        self._tool = tool
        self._selector = dkim_selector

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        request_id, identity_id = _context(run)
        actor = _system_actor(identity_id)
        identity = await self._sending.get(run.tenant_id, identity_id, actor=actor)
        if identity.identity_id != identity_id:
            raise ValidationError("认证 workflow 资源绑定无效")
        request = await self._sending.get_authentication_check_request(
            run.tenant_id, request_id, actor=actor
        )
        if request.sending_identity_id != identity_id:
            raise ValidationError("认证 workflow 请求绑定无效")
        if request.status is AuthenticationCheckRequestStatus.SUCCEEDED:
            return ("complete", None, {})
        if request.status is AuthenticationCheckRequestStatus.FAILED:
            return ("fail", "authentication_failed", {})
        if request.status is AuthenticationCheckRequestStatus.REQUESTED:
            await self._sending.transition_authentication_check_request(
                run.tenant_id,
                request_id,
                AuthenticationCheckRequestStatus.RUNNING,
                actor=actor,
            )
        elif request.status is not AuthenticationCheckRequestStatus.RUNNING:
            raise ValidationError("认证 workflow 请求状态无效")
        facts = await self._tool.check(
            tenant_id=run.tenant_id,
            run_id=run.run_id,
            request_id=request_id,
            sending_identity_id=identity_id,
            domain=identity.domain,
            dkim_selector=self._selector,
        )
        if not isinstance(facts, DnsAuthenticationFacts):
            raise ValidationError("认证工具事实无效")
        await self._sending.record_authentication_result(
            run.tenant_id,
            identity_id,
            _domain_result(facts),
            actor=actor,
        )
        target = (
            AuthenticationCheckRequestStatus.SUCCEEDED
            if facts.all_passed
            else AuthenticationCheckRequestStatus.FAILED
        )
        await self._sending.transition_authentication_check_request(
            run.tenant_id, request_id, target, actor=actor
        )
        if facts.all_passed:
            return ("complete", None, {})
        return ("fail", "authentication_failed", {})


def build_sending_identity_auth_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=WORKFLOW_TYPE,
        version=1,
        steps=(StepDefinition("check", "sending_identity_auth.check"),),
    )


class AuthenticationCheckRequestedHandler:
    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def handle(self, event: AuthenticationCheckRequested) -> None:
        request_id = str(event.request_id)
        identity_id = str(event.sending_identity_id)
        if (
            _REQUEST.fullmatch(request_id) is None
            or _IDENTITY.fullmatch(identity_id) is None
        ):
            raise ValidationError("认证请求事件无效")
        await self._engine.start(
            event.tenant_id,
            WORKFLOW_TYPE,
            request_id,
            {"request_id": request_id, "sending_identity_id": identity_id},
            f"auth:{request_id}",
        )


def register_sending_identity_auth(
    engine: WorkflowEngine, registry: OutboxHandlerRegistry
) -> None:
    engine.register(build_sending_identity_auth_definition())
    registry.register_handler(
        AuthenticationCheckRequested,
        "sending_identity_auth.requested",
        AuthenticationCheckRequestedHandler(engine),  # type: ignore[arg-type]
    )
