"""把获授权任务的受信选择快照装配为六参数 ContextBuilder 调用。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.base import AgentTask
from agent_runtime.context_builder.builder import BuiltContext
from agent_runtime.context_builder.contracts import (
    ContextFactReader,
    ContextIdentity,
    ContextIdentityReader,
    ContextPolicy,
    ContextPolicyReader,
    TaskContextDescriptor,
    TaskContextDescriptorReader,
)
from agent_runtime.context_builder.projection import (
    check_candidate,
    check_ref,
    check_tools,
    positive_limit,
)
from agent_runtime.context_builder.service import ContextBuilderService
from agent_runtime.skill_router.router import SkillRouter
from shared.errors import ValidationError


@dataclass(frozen=True)
class _TaskPolicyReader:
    """不可变任务包装器：技能限制只影响当前任务，不回写共享 provider。"""

    source: ContextPolicyReader
    run_allowed_tools: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    skill_rules: tuple[str, ...]

    async def read(self, identity: ContextIdentity) -> ContextPolicy:
        policy = ContextPolicy.model_validate(await self.source.read(identity))
        check_candidate(policy.model_dump(mode="json"))
        for tools in (
            policy.user_allowed_tools,
            policy.run_allowed_tools,
            policy.explicitly_blocked_tools,
        ):
            check_tools(tools)
        return ContextPolicy.model_validate(
            dict(
                **policy.model_dump(
                    exclude={
                        "run_allowed_tools",
                        "explicitly_blocked_tools",
                        "skill_rules",
                    }
                ),
                run_allowed_tools=tuple(
                    sorted(set(policy.run_allowed_tools) & set(self.run_allowed_tools))
                ),
                explicitly_blocked_tools=tuple(
                    sorted(
                        set(policy.explicitly_blocked_tools) | set(self.blocked_tools)
                    )
                ),
                skill_rules=(*policy.skill_rules, *self.skill_rules),
            )
        )


@dataclass(frozen=True)
class WorkerContextAdapter:
    """通用 worker 的窄适配点；仍须显式注入 policy 和获批 descriptor reader。"""

    descriptors: TaskContextDescriptorReader
    router: SkillRouter
    identities: ContextIdentityReader
    policies: ContextPolicyReader
    facts: ContextFactReader
    registered_tools: tuple[str, ...]
    token_budget: int
    max_token_budget: int
    history_limit: int
    history_byte_limit: int

    def __post_init__(self) -> None:
        for limit in (
            self.token_budget,
            self.max_token_budget,
            self.history_limit,
            self.history_byte_limit,
        ):
            positive_limit(limit)
        if self.token_budget > self.max_token_budget:
            raise ValidationError("上下文预算配置无效")
        check_tools(self.registered_tools)

    async def build(self, task: AgentTask) -> BuiltContext:
        """核对 task 身份与锁版本；inputs 不参与权限、实体选择或预算。"""
        try:
            descriptor = TaskContextDescriptor.model_validate(
                await self.descriptors.read(
                    task.tenant_id, task.run_id, task.acting_user
                )
            )
            if (
                descriptor.tenant_id != task.tenant_id
                or descriptor.run_id != task.run_id
                or descriptor.user_id != task.acting_user
            ):
                raise ValidationError("上下文任务描述身份不匹配")
            selected_ids = tuple(item.skill_id for item in descriptor.skills)
            if (
                not task.skill_ids
                or type(task.skill_ids) is not tuple
                or len(set(selected_ids)) != len(selected_ids)
                or len(set(task.skill_ids)) != len(task.skill_ids)
                or set(selected_ids) != set(task.skill_ids)
            ):
                raise ValidationError("上下文任务技能选择不匹配")
            check_ref(descriptor.source_ref)
            check_tools(descriptor.run_allowed_tools)
            check_tools(descriptor.run_blocked_tools)
            allowed: set[str] = set()
            blocked: set[str] = set(descriptor.run_blocked_tools)
            rules: list[str] = []
            for selected in sorted(descriptor.skills, key=lambda item: item.skill_id):
                manifest = self.router.get(selected.skill_id, selected.version)
                if (
                    manifest.skill_id != selected.skill_id
                    or manifest.version != selected.version
                    or manifest.prompt_ref != selected.prompt_ref
                ):
                    raise ValidationError("上下文技能版本不匹配")
                check_ref(selected.skill_id)
                check_candidate(selected.version)
                check_tools(manifest.allowed_tools)
                check_tools(manifest.blocked_tools)
                check_candidate(selected.prompt)
                check_candidate(manifest.evidence_required)
                allowed.update(manifest.allowed_tools)
                blocked.update(manifest.blocked_tools)
                rules.extend(
                    (
                        f"技能 {selected.skill_id} 版本 {selected.version} 来源 {descriptor.source_ref}",
                        selected.prompt,
                    )
                )
                rules.extend(
                    f"必需证据字段：{name}" for name in manifest.evidence_required
                )
            refs: dict[str, str] = {
                ref.kind: ref.entity_id for ref in descriptor.entity_refs
            }
            if len(refs) != len(descriptor.entity_refs):
                raise ValidationError("上下文实体选择重复")
            builder = ContextBuilderService(
                identities=self.identities,
                policies=_TaskPolicyReader(
                    self.policies,
                    descriptor.run_allowed_tools,
                    tuple(sorted(blocked)),
                    tuple(rules),
                ),
                facts=self.facts,
                registered_tools=self.registered_tools,
                max_token_budget=self.max_token_budget,
                history_limit=self.history_limit,
                history_byte_limit=self.history_byte_limit,
            )
            return await builder.build(
                task.tenant_id,
                task.acting_user,
                task.objective,
                refs,
                tuple(sorted(allowed)),
                self.token_budget,
            )
        except Exception:  # noqa: BLE001 - provider 边界统一脱敏，取消继续传播
            raise ValidationError("上下文任务装配被拒绝") from None
