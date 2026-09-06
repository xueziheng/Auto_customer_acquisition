from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from agent_runtime.base import AgentTask
from agent_runtime.context_builder.contracts import (
    REQUIRED_APPROVAL_ACTIONS,
    ContextEntityRef,
    ContextFact,
    ContextFactSection,
    ContextIdentity,
    ContextPolicy,
    ContextRules,
    ContextSource,
    LockedSkill,
    TaskContextDescriptor,
)
from agent_runtime.context_builder.service import ContextBuilderService
from agent_runtime.skill_router.service import FileSkillRouter
from apps.agent_worker.context_adapter import WorkerContextAdapter
from apps.agent_worker.context_providers import (
    EmployeeContextIdentityReader,
    OpportunityContextFactReader,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.schemas import OpportunityView, ProvenanceSummary
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, RunId, TenantId, UserId

T = TenantId("tenant_a")
U = UserId("user_a")
E = EmployeeId("employee_a")
NOW = datetime(2026, 9, 5, tzinfo=UTC)
TOOLS = ("web.search", "web.fetch", "draft.create")


def identity(**updates: Any) -> ContextIdentity:
    return ContextIdentity(
        tenant_id=T,
        user_id=U,
        employee_id=E,
        role="sales",
        scope_level="self",
        allowed_owner_ids=(E,),
        **updates,
    )


def policy(
    tenant: TenantId = T, employee: EmployeeId = E, **updates: Any
) -> ContextPolicy:
    values: dict[str, Any] = {
        "tenant_id": tenant,
        "employee_id": employee,
        "policy_ref": "policy_v1",
        "user_allowed_tools": ("web.search", "web.fetch"),
        "run_allowed_tools": ("web.search", "draft.create"),
        "explicitly_blocked_tools": (),
        "required_rules": ContextRules(
            playbook_ref="playbook_v1",
            approval_actions=REQUIRED_APPROVAL_ACTIONS,
            exclusions=(),
            directives=("尊重需求证据",),
        ),
        "skill_rules": (),
    }
    values.update(updates)
    return ContextPolicy(**values)


def section(name: str = "opportunity_a", **updates: Any) -> ContextFactSection:
    values: dict[str, Any] = {
        "tenant_id": T,
        "section_id": name,
        "entity_ref": ContextEntityRef(kind="opportunity", entity_id="opportunity_a"),
        "kind": "current_entity_summary",
        "source_ref": "snapshot_a",
        "owner_employee_id": E,
        "facts": (
            ContextFact(
                name="spec_summary",
                value="需要耐腐蚀铰链",
                provenance=ContextSource(
                    source_type="conversation",
                    source_id="message_a",
                    extracted_by="human",
                    extracted_at=NOW,
                    confirmed_by=E,
                    confirmed_at=NOW,
                ),
            ),
        ),
        "inferences": (),
    }
    values.update(updates)
    return ContextFactSection(**values)


class Identities:
    def __init__(self, value: ContextIdentity | None = None) -> None:
        self.value = value or identity()

    async def resolve(
        self, tenant_id: TenantId, acting_user: UserId
    ) -> ContextIdentity:
        return self.value


class Policies:
    def __init__(self, value: ContextPolicy | None = None) -> None:
        self.value = value or policy()

    async def read(self, identity: ContextIdentity) -> ContextPolicy:
        return self.value


class Facts:
    def __init__(self, sections: tuple[ContextFactSection, ...] = ()) -> None:
        self.sections = sections
        self.calls: list[ContextIdentity] = []

    async def load(
        self,
        identity: ContextIdentity,
        refs: tuple[ContextEntityRef, ...],
        *,
        history_limit: int,
    ) -> tuple[ContextFactSection, ...]:
        self.calls.append(identity)
        return self.sections


def builder(
    *,
    facts: Facts | None = None,
    policies: Policies | None = None,
    identities: Identities | None = None,
    history_limit: int = 3,
    history_bytes: int = 10000,
) -> ContextBuilderService:
    return ContextBuilderService(
        identities=identities or Identities(),
        policies=policies or Policies(),
        facts=facts or Facts(),
        registered_tools=TOOLS,
        max_token_budget=20000,
        history_limit=history_limit,
        history_byte_limit=history_bytes,
    )


async def build(
    service: ContextBuilderService,
    budget: int = 10000,
    objective: str = "总结需求",
    tools: tuple[str, ...] = TOOLS,
) -> Any:
    return await service.build(
        T, U, objective, {"opportunity": "opportunity_a"}, tools, budget
    )


@pytest.mark.asyncio
async def test_tools_are_user_run_skill_intersection_and_blocks_override() -> None:
    result = await build(
        builder(policies=Policies(policy(explicitly_blocked_tools=("web.fetch",)))),
        tools=(*TOOLS, "unknown.tool", "web.search"),
    )
    assert result.allowed_tools == ("web.search",)
    assert result.blocked_tools == ("draft.create", "unknown.tool", "web.fetch")
    assert result.data_scope["employee_id"] == E
    assert "allowed_owner_ids" not in result.data_scope
    blocked = await build(
        builder(policies=Policies(policy(explicitly_blocked_tools=("web.search",))))
    )
    assert blocked.allowed_tools == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [True, False, 0, -1, 1.2, "100", 20001])
async def test_invalid_budget_rejected_before_reads(budget: Any) -> None:
    facts = Facts()
    with pytest.raises(ValidationError):
        await build(builder(facts=facts), budget)
    assert facts.calls == []


@pytest.mark.asyncio
async def test_budget_keeps_complete_rules_and_discards_background_with_audit() -> None:
    required = await build(builder(), objective="中文 😀  ")
    actual = len(
        json.dumps(
            {
                "sections": required.sections,
                "allowed_tools": required.allowed_tools,
                "blocked_tools": required.blocked_tools,
                "data_scope": required.data_scope,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert required.token_estimate == actual
    exact = await build(builder(), actual, "中文 😀  ")
    assert exact.sections == required.sections
    with pytest.raises(ValidationError):
        await build(builder(), actual - 1, "中文 😀  ")
    trimmed = await build(builder(facts=Facts((section(),))), actual, "中文 😀  ")
    assert trimmed.sections == required.sections
    assert trimmed.truncation_log and "opportunity_a" in trimmed.truncation_log[0]
    assert "需要耐腐蚀" not in str(trimmed.truncation_log)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location", ["objective", "fact", "discarded", "policy", "source"]
)
async def test_credentials_rejected_before_budget_discard_without_echo(
    location: str,
) -> None:
    marker = "password=placeholder"
    p = policy()
    s = section()
    objective = "总结"
    if location == "objective":
        objective = marker
    elif location == "policy":
        p = policy(skill_rules=(marker,))
    elif location == "source":
        s = section(source_ref=marker)
    else:
        s = section(
            facts=(s.facts[0].model_copy(update={"value": marker}),),
            kind="relevant_history_evidence"
            if location == "discarded"
            else "current_entity_summary",
        )
    with pytest.raises(ValidationError) as caught:
        await build(
            builder(facts=Facts((s,)), policies=Policies(p), history_bytes=1),
            budget=10000,
            objective=objective,
        )
    assert marker not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Contact a@example.com",
        "/private/source.txt",
        "s3://bucket/object",
        "https://host/path?x=value",
        "internal_cost: USD 10",
    ],
)
async def test_private_content_never_enters_context(text: str) -> None:
    with pytest.raises(ValidationError):
        await build(builder(), objective=text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        section(tenant_id=TenantId("tenant_b")),
        section(owner_employee_id=EmployeeId("employee_b")),
        section(
            entity_ref=ContextEntityRef(kind="opportunity", entity_id="opportunity_b")
        ),
    ],
)
async def test_provider_return_scope_is_rechecked(bad: ContextFactSection) -> None:
    with pytest.raises(ValidationError):
        await build(builder(facts=Facts((bad,))))


@pytest.mark.asyncio
async def test_wrong_identity_or_policy_cannot_trigger_business_read() -> None:
    for ids, policies in [
        (
            Identities(
                identity().model_copy(update={"tenant_id": TenantId("tenant_b")})
            ),
            Policies(),
        ),
        (Identities(), Policies(policy(tenant=TenantId("tenant_b")))),
        (Identities(), Policies(policy(employee=EmployeeId("employee_b")))),
    ]:
        facts = Facts()
        with pytest.raises(ValidationError):
            await build(builder(identities=ids, policies=policies, facts=facts))
        assert not facts.calls


@pytest.mark.asyncio
async def test_unknown_entity_and_wildcard_configuration_fail_closed() -> None:
    facts = Facts()
    with pytest.raises(ValidationError):
        await builder(facts=facts).build(T, U, "总结", {"conversation": "c"}, (), 10000)
    assert not facts.calls
    with pytest.raises(ValidationError):
        await build(
            builder(policies=Policies(policy(explicitly_blocked_tools=("web.*",))))
        )


def test_required_rule_coverage_and_strict_dtos() -> None:
    with pytest.raises(PydanticValidationError):
        ContextRules(
            playbook_ref="v1", approval_actions=(), exclusions=(), directives=("规则",)
        )
    with pytest.raises(PydanticValidationError):
        ContextEntityRef(kind="opportunity", entity_id="a", role="boss")
    with pytest.raises(PydanticValidationError):
        ContextFact(name="spec_summary", value="无证据")


class EmployeeViews:
    def __init__(self, records: list[EmployeeView]) -> None:
        self.records = records
        self.calls: list[tuple[TenantId, EmployeeActor]] = []

    async def list_active(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[EmployeeView]:
        self.calls.append((tenant_id, actor))
        return self.records


def employee(
    e: str = "employee_a",
    u: str | None = "user_a",
    tenant: str = "tenant_a",
    role: str = "sales",
    active: bool = True,
    manager: str | None = None,
) -> EmployeeView:
    return EmployeeView(
        employee_id=EmployeeId(e),
        tenant_id=TenantId(tenant),
        name="同名员工",
        user_id=UserId(u) if u else None,
        role=role,
        is_active=active,
        manager_id=EmployeeId(manager) if manager else None,
    )


def employee_reader(
    records: list[EmployeeView], tenant: TenantId = T
) -> EmployeeContextIdentityReader:
    return EmployeeContextIdentityReader(
        tenant_id=tenant,
        employees=EmployeeViews(records),
        lookup_actor=EmployeeActor("lookup_worker", EmployeeScope.SYSTEM, "system"),
    )


@pytest.mark.asyncio
async def test_public_employee_mapping_and_manager_only_active_direct_reports() -> None:
    records = [
        employee(role="manager"),
        employee("employee_b", "user_b", manager="employee_a"),
        employee("employee_c", "user_c", manager="employee_b"),
        employee("employee_d", "user_d", active=False, manager="employee_a"),
    ]
    resolved = await employee_reader(records).resolve(T, U)
    assert resolved.allowed_owner_ids == ("employee_a", "employee_b")
    assert resolved.scope_level == "manager"
    sales = await employee_reader(records).resolve(T, UserId("user_b"))
    assert sales.allowed_owner_ids == ("employee_b",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [
        [],
        [employee(u=None)],
        [employee(u="employee_a")],
        [employee(), employee("duplicate")],
        [employee(active=False)],
        [employee(tenant="tenant_b")],
        [employee(role="superadmin")],
    ],
)
async def test_employee_mapping_rejects_missing_fallback_duplicate_inactive_and_cross_tenant(
    records: list[EmployeeView],
) -> None:
    with pytest.raises(ValidationError):
        await employee_reader(records).resolve(T, U)


class Opportunities:
    def __init__(self, view: OpportunityView) -> None:
        self.view = view
        self.calls: list[Any] = []

    async def get(
        self, tenant_id: TenantId, opportunity_id: Any, *, actor: Any
    ) -> OpportunityView:
        self.calls.append((tenant_id, opportunity_id, actor))
        assert actor.actor_id == "employee_a"
        assert actor.scope.allowed_owners == frozenset({"employee_a"})
        return self.view


def opportunity(**updates: Any) -> OpportunityView:
    args: dict[str, Any] = {
        "opportunity_id": "opportunity_a",
        "account_id": "account_a",
        "account_name": "不投影的客户",
        "country": "DE",
        "need_id": "need_a",
        "product_category": "五金",
        "state": "assigned",
        "created_at": NOW,
        "owner": "employee_a",
        "spec_summary": "耐腐蚀铰链",
        "provenance": [
            ProvenanceSummary(
                "spec_summary",
                "conversation",
                "message_a",
                "human",
                NOW,
                "employee_a",
                NOW,
                None,
                None,
            )
        ],
    }
    args.update(updates)
    return OpportunityView(**args)


@pytest.mark.asyncio
async def test_opportunity_adapter_supplies_abac_before_precise_public_get() -> None:
    public = Opportunities(opportunity())
    reader = OpportunityContextFactReader(tenant_id=T, opportunities=public)
    result = await reader.load(
        identity(),
        (ContextEntityRef(kind="opportunity", entity_id="opportunity_a"),),
        history_limit=3,
    )
    assert public.calls[0][:2] == (T, "opportunity_a")
    assert result[0].facts[0].value == "耐腐蚀铰链"
    assert result[0].facts[0].provenance.source_id == "message_a"
    assert "account_name" not in result[0].model_dump()
    assert result[0].tenant_id == T


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "view",
    [
        opportunity(owner="employee_b"),
        opportunity(opportunity_id="other"),
        opportunity(provenance=[]),
        opportunity(
            provenance=[
                ProvenanceSummary(
                    "spec_summary",
                    "agent_inference",
                    "message_a",
                    "model_v1",
                    NOW,
                    None,
                    None,
                    None,
                    None,
                )
            ]
        ),
    ],
)
async def test_opportunity_adapter_rejects_wrong_owner_id_or_unproven_fact(
    view: OpportunityView,
) -> None:
    reader = OpportunityContextFactReader(
        tenant_id=T, opportunities=Opportunities(view)
    )
    with pytest.raises(ValidationError):
        await reader.load(
            identity(),
            (ContextEntityRef(kind="opportunity", entity_id="opportunity_a"),),
            history_limit=3,
        )


class Descriptors:
    def __init__(self, descriptor: TaskContextDescriptor) -> None:
        self.descriptor = descriptor

    async def read(
        self, tenant_id: TenantId, run_id: RunId, acting_user: UserId
    ) -> TaskContextDescriptor:
        await asyncio.sleep(0)
        return self.descriptor


def descriptor(**updates: Any) -> TaskContextDescriptor:
    values = {
        "tenant_id": T,
        "run_id": RunId("run_a"),
        "user_id": U,
        "source_ref": "approved_selection_a",
        "run_allowed_tools": TOOLS,
        "run_blocked_tools": (),
        "skills": (
            LockedSkill(
                skill_id="qualification.review",
                version="1.0.0",
                prompt_ref="prompt.md",
                prompt="请按原始证据总结",
            ),
        ),
        "entity_refs": (
            ContextEntityRef(kind="opportunity", entity_id="opportunity_a"),
        ),
    }
    values.update(updates)
    return TaskContextDescriptor(**values)


def router(tmp_path: Path) -> FileSkillRouter:
    directory = tmp_path / "canonical" / "qualification.review"
    directory.mkdir(parents=True)
    (directory / "prompt.md").write_text("请按原始证据总结")
    (directory / "manifest.yaml").write_text("""skill_id: qualification.review
version: 1.0.0
domain: qualification
description: 受控技能
triggers: [review]
inputs: {}
outputs: {}
allowed_tools: [web.search, web.fetch]
blocked_tools: [web.search]
evidence_required: [source_id]
risk_level: low
cost_class: low
prompt_ref: prompt.md
eval_refs: [evals/fixture]
evals: []
""")
    result = FileSkillRouter()
    result.load_registry(str(tmp_path))
    return result


def adapter(
    tmp_path: Path, desc: TaskContextDescriptor | None = None
) -> WorkerContextAdapter:
    return WorkerContextAdapter(
        descriptors=Descriptors(desc or descriptor()),
        router=router(tmp_path),
        identities=Identities(),
        policies=Policies(),
        facts=Facts((section(),)),
        registered_tools=TOOLS,
        token_budget=10000,
        max_token_budget=20000,
        history_limit=3,
        history_byte_limit=10000,
    )


def task(**updates: Any) -> AgentTask:
    values = {
        "tenant_id": T,
        "run_id": RunId("run_a"),
        "acting_user": U,
        "objective": "总结需求",
        "skill_ids": ("qualification.review",),
        "inputs": {
            "role": "boss",
            "employee_id": "employee_b",
            "allowed_tools": ["web.search"],
            "token_budget": 1,
            "entity_refs": {"conversation": "other"},
        },
    }
    values.update(updates)
    return AgentTask(**values)


@pytest.mark.asyncio
async def test_adapter_locks_manifest_version_and_ignores_self_authorizing_inputs(
    tmp_path: Path,
) -> None:
    result = await adapter(tmp_path).build(task())
    assert result.allowed_tools == ()
    assert result.blocked_tools == ("web.fetch", "web.search")
    assert result.data_scope["employee_id"] == E
    assert "请按原始证据总结" in str(result.sections)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("tenant_id", "tenant_b"), ("run_id", "run_b"), ("user_id", "user_b")],
)
async def test_descriptor_wrong_binding_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    with pytest.raises(ValidationError):
        await adapter(tmp_path, descriptor().model_copy(update={field: value})).build(
            task()
        )


@pytest.mark.asyncio
async def test_adapter_prompt_guard_and_unknown_or_unlocked_skills(
    tmp_path: Path,
) -> None:
    runtime = adapter(tmp_path)
    for changed in [task(skill_ids=()), task(skill_ids=("unknown.skill",))]:
        with pytest.raises(ValidationError):
            await runtime.build(changed)
    with pytest.raises(PydanticValidationError):
        LockedSkill(
            skill_id="qualification.review",
            version="",
            prompt_ref="prompt.md",
            prompt="规则",
        )
    runtime = WorkerContextAdapter(
        descriptors=Descriptors(
            descriptor(
                skills=(
                    LockedSkill(
                        skill_id="qualification.review",
                        version="1.0.0",
                        prompt_ref="prompt.md",
                        prompt="password=placeholder",
                    ),
                )
            )
        ),
        router=runtime.router,
        identities=Identities(),
        policies=Policies(),
        facts=Facts(),
        registered_tools=TOOLS,
        token_budget=10000,
        max_token_budget=20000,
        history_limit=3,
        history_byte_limit=10000,
    )
    with pytest.raises(ValidationError):
        await runtime.build(task())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["资料 bucket/object/file.pdf", "内部成本为 20 美元", "estimated cost is USD 10"],
)
async def test_relative_locators_and_cost_prose_are_not_model_candidates(
    text: str,
) -> None:
    with pytest.raises(ValidationError):
        await build(builder(), objective=text)


def test_whitespace_cannot_masquerade_as_required_playbook_rules() -> None:
    with pytest.raises(PydanticValidationError):
        ContextRules(
            playbook_ref="v1",
            approval_actions=REQUIRED_APPROVAL_ACTIONS,
            exclusions=(),
            directives=("   ",),
        )


@pytest.mark.asyncio
async def test_invalid_web_source_is_not_upgraded_to_provenance() -> None:
    view = opportunity(
        provenance=[
            ProvenanceSummary(
                "spec_summary",
                "web_page",
                "snapshot_a",
                "human",
                NOW,
                E,
                NOW,
                None,
                None,
            )
        ]
    )
    with pytest.raises(ValidationError):
        await OpportunityContextFactReader(
            tenant_id=T, opportunities=Opportunities(view)
        ).load(
            identity(),
            (ContextEntityRef(kind="opportunity", entity_id="opportunity_a"),),
            history_limit=3,
        )


@pytest.mark.asyncio
async def test_history_limits_drop_complete_sections_in_stable_order() -> None:
    sections = tuple(
        section(name=name, kind="relevant_history_evidence")
        for name in ("history_c", "history_a", "history_b")
    )
    result = await build(builder(facts=Facts(sections), history_limit=1))
    histories = [
        item for item in result.sections if item["kind"] == "relevant_history_evidence"
    ]
    assert [item["section_id"] for item in histories] == ["history_a"]
    assert histories[0]["facts"][0]["provenance"]["source_id"] == "message_a"
    assert result.truncation_log == [
        "history_b：历史上限，整段移除",
        "history_c：历史上限，整段移除",
    ]
    byte_limited = await build(builder(facts=Facts(sections), history_bytes=1))
    assert len(byte_limited.sections) == 2
    assert len(byte_limited.truncation_log) == 3


@pytest.mark.asyncio
async def test_unconfigured_policy_fails_before_facts_and_context_output() -> None:
    facts = Facts()
    with pytest.raises(ValidationError):
        await ContextBuilderService(
            identities=Identities(),
            policies=None,
            facts=facts,
            registered_tools=TOOLS,
            max_token_budget=20000,
            history_limit=3,
            history_byte_limit=10000,
        ).build(T, U, "任务", {}, (), 10000)
    assert not facts.calls


@pytest.mark.asyncio
async def test_adapter_concurrent_tenants_cannot_share_policy_or_mutable_results(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    first = adapter(tmp_path)
    second_tenant = TenantId("tenant_b")
    second_user = UserId("user_b")
    second_employee = EmployeeId("employee_b")
    second_identity = ContextIdentity(
        tenant_id=second_tenant,
        user_id=second_user,
        employee_id=second_employee,
        role="sales",
        scope_level="self",
        allowed_owner_ids=(second_employee,),
    )
    shared_policy_reads: list[str] = []

    class TenantPolicies:
        async def read(self, identity: ContextIdentity) -> ContextPolicy:
            shared_policy_reads.append(identity.tenant_id)
            await asyncio.sleep(0)
            return policy(
                identity.tenant_id,
                identity.employee_id,
                run_allowed_tools=("web.fetch",)
                if identity.tenant_id == second_tenant
                else ("web.search",),
            )

    policies = TenantPolicies()
    first = replace(first, policies=policies)
    second = replace(
        first,
        policies=policies,
        identities=Identities(second_identity),
        descriptors=Descriptors(
            descriptor(tenant_id=second_tenant, user_id=second_user)
        ),
        facts=Facts(
            (section(tenant_id=second_tenant, owner_employee_id=second_employee),)
        ),
    )
    a, b = await asyncio.gather(
        first.build(task()),
        second.build(task(tenant_id=second_tenant, acting_user=second_user)),
    )
    assert a.allowed_tools == ()
    assert b.allowed_tools == ("web.fetch",)
    assert a.data_scope["tenant_id"] == T and b.data_scope["tenant_id"] == second_tenant
    assert set(shared_policy_reads) == {"tenant_a", "tenant_b"}
    b.data_scope["employee_id"] = "boss"
    b.sections.clear()
    rebuilt = await second.build(task(tenant_id=second_tenant, acting_user=second_user))
    assert rebuilt.data_scope["employee_id"] == second_employee
    assert rebuilt.sections
    assert a.sections


@pytest.mark.asyncio
async def test_two_sales_read_only_owned_opportunity_through_real_domain_services() -> (
    None
):
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from types import SimpleNamespace

    from domains.employees.permissions import Phase1EmployeeAuthorizer
    from domains.employees.service_impl import EmployeeServiceImpl
    from domains.opportunities.permissions import Phase1OpportunityAuthorizer
    from domains.opportunities.service_impl import OpportunityServiceImpl
    from shared.schemas.identifiers import OpportunityId
    from shared.schemas.provenance import Provenance, SourceType
    from tests.unit.test_employees_service import _emp
    from tests.unit.test_opportunities_service import _opp

    order: list[str] = []
    employees = [
        replace(
            _emp(f"employee_{n}"), tenant_id=TenantId(t), user_id=UserId(f"user_{n}")
        )
        for n, t in [("a", "tenant_a"), ("b", "tenant_a"), ("c", "tenant_b")]
    ]
    opportunities = [
        _opp(
            f"opportunity_{n}",
            t,
            f"need_{n}",
            owner=EmployeeId(f"employee_{n}"),
            spec_summary=f"规格 {n}",
        )
        for n, t in [("a", "tenant_a"), ("b", "tenant_a"), ("c", "tenant_b")]
    ]

    class Audit:
        def log(self, **fields: Any) -> None:
            order.append("audit")

    class Employees:
        async def list_active(self, tenant_id: TenantId) -> list[Any]:
            order.append("employees_read")
            return [
                row for row in employees if row.tenant_id == tenant_id and row.is_active
            ]

    class OpportunityRepo:
        async def get(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> Any:
            order.append("opportunity_read")
            return next(
                (
                    row
                    for row in opportunities
                    if row.tenant_id == tenant_id
                    and row.opportunity_id == opportunity_id
                ),
                None,
            )

    class Sources:
        async def list_for_entity(
            self, tenant_id: TenantId, kind: str, entity_id: str
        ) -> list[Any]:
            order.append("provenance_read")
            return [
                (
                    "spec_summary",
                    Provenance(SourceType.CONVERSATION, "message_a", "human", NOW),
                )
            ]

    async def no_snapshot(*args: Any) -> None:
        return None

    async def no_count(*args: Any) -> int:
        return 0

    class Authorizer(Phase1OpportunityAuthorizer):
        def require(
            self, actor: Any, action: Any, scope: Any, tenant_id: TenantId
        ) -> str:
            result = super().require(actor, action, scope, tenant_id)
            order.append("opportunity_authorized")
            return result

    @asynccontextmanager
    async def uow() -> Any:
        yield SimpleNamespace(
            opportunities=OpportunityRepo(),
            snapshots=SimpleNamespace(latest_for_opportunity=no_snapshot),
            handoffs=SimpleNamespace(find_pending_for_opportunity=no_snapshot),
            provenance=Sources(),
        )

    for tenant, user, owner in [
        ("tenant_a", "user_a", "a"),
        ("tenant_a", "user_b", "b"),
        ("tenant_b", "user_c", "c"),
    ]:
        tenant_id = TenantId(tenant)
        employees_service = EmployeeServiceImpl(
            employees=Employees(),
            territories=None,
            ownership=None,
            now=lambda: NOW,
            manager_pool=lambda _: (),
            count_active_accounts=no_count,
            authorizer=Phase1EmployeeAuthorizer(tenant_id),
            audit=Audit(),
        )
        identity_reader = EmployeeContextIdentityReader(
            tenant_id=tenant_id,
            employees=employees_service,
            lookup_actor=EmployeeActor("lookup_worker", EmployeeScope.SYSTEM, "system"),
        )
        facts_reader = OpportunityContextFactReader(
            tenant_id=tenant_id,
            opportunities=OpportunityServiceImpl(
                uow, None, None, authorizer=Authorizer(tenant_id), audit=Audit()
            ),
        )
        service = ContextBuilderService(
            identities=identity_reader,
            policies=Policies(policy(tenant_id, EmployeeId(f"employee_{owner}"))),
            facts=facts_reader,
            registered_tools=TOOLS,
            max_token_budget=20000,
            history_limit=3,
            history_byte_limit=10000,
        )
        order.clear()
        result = await service.build(
            tenant_id,
            UserId(user),
            "总结",
            {"opportunity": f"opportunity_{owner}"},
            TOOLS,
            10000,
        )
        assert result.sections[-1]["facts"][0]["value"] == f"规格 {owner}"
        assert order.index("audit") < order.index("employees_read")
        assert (
            order.index("opportunity_authorized")
            < order.index("opportunity_read")
            < order.index("provenance_read")
        )
        order.clear()
        foreign = "opportunity_b" if owner != "b" else "opportunity_a"
        with pytest.raises(ValidationError):
            await service.build(
                tenant_id, UserId(user), "总结", {"opportunity": foreign}, TOOLS, 10000
            )
        assert "provenance_read" not in order


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [False, True])
async def test_worker_consumes_actual_built_context_and_sends_guarded_change_set_to_gate(
    tmp_path: Path, unsafe: bool
) -> None:
    from agent_runtime.base import ChangeSet
    from agent_runtime.context_builder.builder import BuiltContext
    from apps.agent_worker.main import (
        AgentJob,
        AgentWorkerConfig,
        AgentWorkerRuntime,
        run_agent_worker,
    )
    from shared.schemas.identifiers import ChangeSetId
    from tests.unit.test_agent_worker import Gate, Jobs

    stop = asyncio.Event()
    consumed: list[str] = []

    class Consumer:
        name = "demand_intelligence"

        async def run(self, task: AgentTask, context: object) -> ChangeSet:
            assert isinstance(context, BuiltContext)
            fact = context.sections[-1]["facts"][0]["value"]
            consumed.append(fact)
            stop.set()
            changes = [
                {
                    "domain": "outreach",
                    "operation": "create_draft",
                    "risk_level": "high",
                    "payload": {
                        "subject": "Follow up",
                        "body": "The price is USD 2.50."
                        if unsafe
                        else "Could you confirm the specification?",
                        "target_language": "en",
                        "content_language": "en",
                    },
                }
            ]
            return ChangeSet(
                ChangeSetId("change_a"),
                task.tenant_id,
                task.run_id,
                changes=changes,
                summary=f"已读取规格：{fact}",
            )

    gate = Gate()
    jobs = Jobs(queued=[AgentJob("context_job", "demand_intelligence", task())])
    result = await run_agent_worker(
        AgentWorkerRuntime(
            jobs=jobs,
            agents={"demand_intelligence": Consumer()},
            contexts=adapter(tmp_path),
            gate=gate,
            config=AgentWorkerConfig(batch_limit=1, idle_seconds=1),
        ),
        stop_event=stop,
        install_signal_handlers=False,
    )
    assert consumed == ["需要耐腐蚀铰链"]
    assert result.jobs_completed == 1
    assert len(gate.accepted) == 1
    if unsafe:
        assert gate.accepted[0].changes == []
        assert gate.accepted[0].guardrail_violations
    else:
        assert gate.accepted[0].changes[0]["risk_level"] == "high"
        assert gate.accepted[0].summary == "已读取规格：需要耐腐蚀铰链"
    # 此记录端口只证明原 gate 获得高风险变更，未证明审批落库或自动批准。


@pytest.mark.asyncio
async def test_same_user_concurrent_runs_apply_their_own_locked_tool_permissions(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    class RunDescriptors:
        async def read(
            self, tenant_id: TenantId, run_id: RunId, acting_user: UserId
        ) -> TaskContextDescriptor:
            await asyncio.sleep(0)
            return descriptor(
                run_id=run_id,
                run_allowed_tools=("web.fetch",),
                run_blocked_tools=("web.fetch",) if run_id == "run_b" else (),
            )

    runtime = replace(
        adapter(tmp_path),
        descriptors=RunDescriptors(),
        policies=Policies(policy(run_allowed_tools=("web.fetch",))),
    )
    first, second = await asyncio.gather(
        runtime.build(task()), runtime.build(task(run_id=RunId("run_b")))
    )
    assert first.allowed_tools == ("web.fetch",)
    assert second.allowed_tools == ()
    assert second.blocked_tools == ("web.fetch", "web.search")
    # Run 授权不能来自任务输入，也不能被后一项任务改写共享 policy。
    again = await runtime.build(task(inputs={"run_blocked_tools": ["web.fetch"]}))
    assert again.allowed_tools == ("web.fetch",)


@pytest.mark.asyncio
async def test_adapter_uses_locked_historical_version_after_registry_upgrade(
    tmp_path: Path,
) -> None:
    runtime = adapter(tmp_path)
    root = tmp_path / "canonical" / "qualification.review"
    newer = root / "2.0.0"
    newer.mkdir()
    (newer / "prompt.md").write_text("新版本规则")
    (newer / "manifest.yaml").write_text(
        (root / "manifest.yaml")
        .read_text()
        .replace("version: 1.0.0", "version: 2.0.0")
        .replace("blocked_tools: [web.search]", "blocked_tools: []")
    )
    runtime.router.load_registry(str(tmp_path))
    assert runtime.router.get("qualification.review").version == "2.0.0"
    built = await runtime.build(task())
    assert built.allowed_tools == ()
    assert "版本 1.0.0" in str(built.sections)
    assert "版本 2.0.0" not in str(built.sections)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version,prompt_ref", [("9.0.0", "prompt.md"), ("1.0.0", "different.md")]
)
async def test_missing_locked_version_or_wrong_prompt_asset_is_rejected(
    tmp_path: Path, version: str, prompt_ref: str
) -> None:
    selected = (
        descriptor()
        .skills[0]
        .model_copy(update={"version": version, "prompt_ref": prompt_ref})
    )
    with pytest.raises(ValidationError):
        await adapter(tmp_path, descriptor(skills=(selected,))).build(task())


@pytest.mark.asyncio
async def test_modified_model_audit_scope_does_not_authorize_gateway_tool() -> None:
    from types import SimpleNamespace

    from tool_gateway.checks.permission import PermissionCheck
    from tool_gateway.pipeline import ToolCallContext, ToolInvocationState

    context = await build(builder())
    context.data_scope["role"] = "boss"
    context.data_scope["allowed_tools"] = ["draft.create"]
    checked: list[tuple[str, str, str]] = []

    async def trusted_authorize(
        ctx: ToolCallContext, state: ToolInvocationState
    ) -> bool:
        checked.append((ctx.tenant_id, ctx.user_id, ctx.tool_id))
        return ctx.user_id == U and ctx.tool_id == "web.search"

    denial = await PermissionCheck(trusted_authorize).check(
        ToolCallContext(T, U, "draft.create", {"data_scope": context.data_scope}),
        ToolInvocationState(SimpleNamespace(), "call_a"),
    )
    assert denial is not None and denial.stage == "permission"
    assert checked == [("tenant_a", "user_a", "draft.create")]


@pytest.mark.asyncio
async def test_credential_in_fact_means_zero_consumer_calls(tmp_path: Path) -> None:
    from dataclasses import replace

    runtime = adapter(tmp_path)
    unsafe_fact = (
        section().facts[0].model_copy(update={"value": "password=placeholder"})
    )
    runtime = replace(
        runtime,
        facts=Facts((section(facts=(unsafe_fact,), kind="relevant_history_evidence"),)),
        history_byte_limit=1,
    )
    calls: list[object] = []
    with pytest.raises(ValidationError):
        context = await runtime.build(task())
        calls.append(context)
    assert not calls


@pytest.mark.asyncio
async def test_policy_wildcard_cannot_be_hidden_by_run_intersection(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    runtime = replace(
        adapter(tmp_path), policies=Policies(policy(run_allowed_tools=("web.*",)))
    )
    with pytest.raises(ValidationError):
        await runtime.build(task())


@pytest.mark.asyncio
async def test_locked_semver_build_metadata_uses_exact_registered_version(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    runtime = adapter(tmp_path)
    manifest = tmp_path / "canonical" / "qualification.review" / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace("1.0.0", "1.0.0+approved"))
    runtime.router.load_registry(str(tmp_path))
    selected = descriptor().skills[0].model_copy(update={"version": "1.0.0+approved"})
    runtime = replace(runtime, descriptors=Descriptors(descriptor(skills=(selected,))))
    result = await runtime.build(task())
    assert "1.0.0+approved" in str(result.sections)
