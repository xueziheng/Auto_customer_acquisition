"""按受信主体、规则和事实构建可审计的受限上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime.context_builder.builder import LOADING_PRIORITY, BuiltContext
from agent_runtime.context_builder.contracts import (
    ContextEntityRef,
    ContextFactReader,
    ContextFactSection,
    ContextIdentity,
    ContextIdentityReader,
    ContextPolicy,
    ContextPolicyReader,
)
from agent_runtime.context_builder.projection import (
    check_candidate,
    check_ref,
    check_tools,
    json_bytes,
    model_payload,
    positive_limit,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, UserId


@dataclass(frozen=True)
class ContextBuilderService:
    """每任务不可变装配；数据范围只用于审计，Gateway 独立判权。"""

    identities: ContextIdentityReader
    policies: ContextPolicyReader
    facts: ContextFactReader
    registered_tools: tuple[str, ...]
    max_token_budget: int
    history_limit: int
    history_byte_limit: int

    def __post_init__(self) -> None:
        for limit in (
            self.max_token_budget,
            self.history_limit,
            self.history_byte_limit,
        ):
            positive_limit(limit)
        check_tools(self.registered_tools)

    async def build(
        self,
        tenant_id: TenantId,
        acting_user: UserId,
        task_objective: str,
        entity_refs: dict[str, str],
        skill_tool_requirements: tuple[str, ...],
        token_budget: int,
    ) -> BuiltContext:
        """身份与策略先于事实 IO，完整扫描候选后才按段裁剪背景。"""
        try:
            return await self._build(
                tenant_id,
                acting_user,
                task_objective,
                entity_refs,
                skill_tool_requirements,
                token_budget,
            )
        except Exception:  # noqa: BLE001 - provider 边界统一脱敏，取消继续传播
            # provider 异常可能携带原文或内部定位，公开边界只保留固定摘要。
            raise ValidationError("上下文构建被拒绝") from None

    async def _build(
        self,
        tenant_id: TenantId,
        acting_user: UserId,
        task_objective: str,
        entity_refs: dict[str, str],
        skill_tool_requirements: tuple[str, ...],
        token_budget: int,
    ) -> BuiltContext:
        positive_limit(token_budget)
        if (
            token_budget > self.max_token_budget
            or type(task_objective) is not str
            or not task_objective.strip()
        ):
            raise ValidationError("上下文输入无效")
        check_ref(tenant_id)
        check_ref(acting_user)
        check_candidate(task_objective)
        check_tools(skill_tool_requirements)
        if type(entity_refs) is not dict:
            raise ValidationError("上下文实体引用无效")
        refs = tuple(
            ContextEntityRef.model_validate({"kind": kind, "entity_id": entity_id})
            for kind, entity_id in sorted(entity_refs.items())
        )
        for ref in refs:
            check_ref(ref.entity_id)
        identity = ContextIdentity.model_validate(
            await self.identities.resolve(tenant_id, acting_user)
        )
        if identity.tenant_id != tenant_id or identity.user_id != acting_user:
            raise ValidationError("上下文身份不匹配")
        for owner_id in (identity.employee_id, *identity.allowed_owner_ids):
            check_ref(owner_id)
        policy = ContextPolicy.model_validate(await self.policies.read(identity))
        if policy.tenant_id != tenant_id or policy.employee_id != identity.employee_id:
            raise ValidationError("上下文策略不匹配")
        check_ref(policy.policy_ref)
        check_ref(policy.required_rules.playbook_ref)
        check_candidate(policy.model_dump(mode="json"))
        for tools in (
            policy.user_allowed_tools,
            policy.run_allowed_tools,
            policy.explicitly_blocked_tools,
        ):
            check_tools(tools)
        requested = set(skill_tool_requirements)
        allowed = tuple(
            sorted(
                requested
                & set(policy.user_allowed_tools)
                & set(policy.run_allowed_tools)
                & set(self.registered_tools) - set(policy.explicitly_blocked_tools)
            )
        )
        blocked = tuple(
            sorted(set(policy.explicitly_blocked_tools) | (requested - set(allowed)))
        )
        scope: dict[str, Any] = {
            "tenant_id": tenant_id,
            "employee_id": identity.employee_id,
            "role": identity.role,
            "scope_level": identity.scope_level,
            "territory_status": identity.territory_status,
        }
        sections: list[dict[str, Any]] = [
            {"kind": "task_objective_and_directive", "objective": task_objective},
            {
                "kind": "playbook_rules",
                "policy_ref": policy.policy_ref,
                "rules": policy.required_rules.model_dump(mode="json"),
                "skill_rules": list(policy.skill_rules),
            },
        ]
        if json_bytes(model_payload(sections, allowed, blocked, scope)) > token_budget:
            raise ValidationError("必需上下文超出预算")
        loaded = await self.facts.load(identity, refs, history_limit=self.history_limit)
        if type(loaded) is not tuple:
            raise ValidationError("上下文事实集合无效")
        optional: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in loaded:
            item = ContextFactSection.model_validate(raw)
            if (
                item.tenant_id != tenant_id
                or item.owner_employee_id not in identity.allowed_owner_ids
                or item.entity_ref not in refs
                or item.section_id in seen
            ):
                raise ValidationError("上下文事实范围无效")
            seen.add(item.section_id)
            candidate = item.model_dump(mode="json")
            check_candidate(candidate)
            for value in (
                item.section_id,
                item.source_ref,
                item.entity_ref.entity_id,
                item.owner_employee_id,
            ):
                check_ref(value)
            for fact in item.facts:
                check_ref(fact.provenance.source_id)
                check_ref(fact.provenance.extracted_by)
                if fact.provenance.confirmed_by is not None:
                    check_ref(fact.provenance.confirmed_by)
            optional.append(candidate)
        optional.sort(
            key=lambda item: (LOADING_PRIORITY.index(item["kind"]), item["section_id"])
        )
        truncation: list[str] = []
        history_count = 0
        history_bytes = 0
        for candidate in optional:
            if candidate["kind"] == "relevant_history_evidence":
                size = json_bytes(candidate)
                if (
                    history_count >= self.history_limit
                    or history_bytes + size > self.history_byte_limit
                ):
                    truncation.append(f"{candidate['section_id']}：历史上限，整段移除")
                    continue
                history_count += 1
                history_bytes += size
            sections.append(candidate)
        while (
            json_bytes(model_payload(sections, allowed, blocked, scope)) > token_budget
        ):
            removed = sections.pop()
            truncation.append(f"{removed['section_id']}：预算上限，整段移除")
        return BuiltContext(
            sections=sections,
            allowed_tools=allowed,
            blocked_tools=blocked,
            data_scope=scope,
            token_estimate=json_bytes(model_payload(sections, allowed, blocked, scope)),
            truncation_log=truncation,
        )
