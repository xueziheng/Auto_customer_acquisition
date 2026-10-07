"""Worker 的公开域服务适配器；原始名单与内部成本不进入上下文。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.context_builder.contracts import (
    ContextEntityRef,
    ContextFact,
    ContextFactSection,
    ContextIdentity,
    ContextSource,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.service import EmployeeService
from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.opportunities.service import OpportunityService
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, OpportunityId, TenantId, UserId


@dataclass(frozen=True)
class EmployeeContextIdentityReader:
    """经员工公共 DTO 的 user_id 唯一映射；lookup actor 只读取员工名单。"""

    tenant_id: TenantId
    employees: EmployeeService
    lookup_actor: EmployeeActor

    def __post_init__(self) -> None:
        if (
            self.lookup_actor.role != "system"
            or self.lookup_actor.scope is not EmployeeScope.SYSTEM
        ):
            raise ValidationError("上下文员工读取身份无效")

    async def resolve(
        self, tenant_id: TenantId, acting_user: UserId
    ) -> ContextIdentity:
        """不信任任务自报员工身份；经理仅自身及活跃直属员工。"""
        try:
            if tenant_id != self.tenant_id:
                raise ValidationError("上下文员工租户不匹配")
            records = await self.employees.list_active(
                tenant_id, actor=self.lookup_actor
            )
            if any(row.tenant_id != tenant_id for row in records):
                raise ValidationError("上下文员工租户不匹配")
            matches = [row for row in records if row.user_id == acting_user]
            if len(matches) != 1 or not matches[0].is_active:
                raise ValidationError("上下文员工映射无效")
            current = matches[0]
            if current.role not in {"sales", "manager", "boss"}:
                raise ValidationError("上下文员工角色无效")
            active = [row for row in records if row.is_active]
            if len({row.employee_id for row in active}) != len(active):
                raise ValidationError("上下文员工映射无效")
            owners = {current.employee_id}
            if current.role == "manager":
                owners.update(
                    row.employee_id
                    for row in active
                    if row.manager_id == current.employee_id
                )
            elif current.role == "boss":
                owners.update(row.employee_id for row in active)
            return ContextIdentity.model_validate(
                {
                    "tenant_id": tenant_id,
                    "user_id": acting_user,
                    "employee_id": current.employee_id,
                    "role": current.role,
                    "scope_level": {
                        "sales": "self",
                        "manager": "manager",
                        "boss": "tenant",
                    }[current.role],
                    "allowed_owner_ids": tuple(sorted(owners)),
                }
            )
        except Exception:  # noqa: BLE001 - provider 边界统一脱敏，取消继续传播
            raise ValidationError("上下文员工映射被拒绝") from None


@dataclass(frozen=True)
class OpportunityContextFactReader:
    """精确读取机会公共 View；服务先执行 tenant/ABAC，本适配器再核 ID/owner。"""

    tenant_id: TenantId
    opportunities: OpportunityService

    async def load(
        self,
        identity: ContextIdentity,
        refs: tuple[ContextEntityRef, ...],
        *,
        history_limit: int,
    ) -> tuple[ContextFactSection, ...]:
        """只投影带来源的需求关键字段；本 adapter 不加载历史或供应资料。"""
        try:
            identity = ContextIdentity.model_validate(identity)
            if identity.tenant_id != self.tenant_id:
                raise ValidationError("上下文机会租户不匹配")
            checked_refs = tuple(ContextEntityRef.model_validate(ref) for ref in refs)
            scope = OpportunityScope(
                level=ScopeLevel(identity.scope_level),
                allowed_owners=frozenset(identity.allowed_owner_ids),
            )
            actor = Actor(
                actor_id=identity.employee_id, scope=scope, role=identity.role
            )
            sections: list[ContextFactSection] = []
            for ref in checked_refs:
                view = await self.opportunities.get(
                    self.tenant_id, OpportunityId(ref.entity_id), actor=actor
                )
                if (
                    view.opportunity_id != ref.entity_id
                    or view.owner not in identity.allowed_owner_ids
                ):
                    raise ValidationError("上下文机会范围不匹配")
                facts: list[ContextFact] = []
                for name in ("quantity", "spec_summary", "destination", "required_by"):
                    value = getattr(view, name)
                    if value is None:
                        continue
                    provenance = [
                        item for item in view.provenance if item.field_name == name
                    ]
                    if len(provenance) != 1:
                        raise ValidationError("上下文机会缺少唯一来源")
                    source = provenance[0]
                    if source.source_type == "web_page" and (
                        not source.source_url
                        or not source.source_url.strip()
                        or not source.page_hash
                        or not source.page_hash.strip()
                    ):
                        raise ValidationError("上下文网页来源不完整")
                    projected_source = ContextSource.model_validate(
                        {
                            "source_type": source.source_type,
                            "source_id": source.source_id,
                            "extracted_by": source.extracted_by,
                            "extracted_at": source.extracted_at,
                            "confirmed_by": source.confirmed_by,
                            "confirmed_at": source.confirmed_at,
                        }
                    )
                    facts.append(
                        ContextFact.model_validate(
                            {
                                "name": name,
                                "value": value.isoformat()
                                if name == "required_by"
                                else value,
                                "provenance": projected_source,
                            }
                        )
                    )
                if not facts:
                    raise ValidationError("上下文机会没有可投影事实")
                sections.append(
                    ContextFactSection(
                        tenant_id=self.tenant_id,
                        section_id=ref.entity_id,
                        entity_ref=ref,
                        kind="current_entity_summary",
                        source_ref=ref.entity_id,
                        owner_employee_id=EmployeeId(view.owner),
                        facts=tuple(facts),
                        inferences=(),
                    )
                )
            return tuple(sections)
        except Exception:  # noqa: BLE001 - provider 边界统一脱敏，取消继续传播
            raise ValidationError("上下文机会读取被拒绝") from None
