"""国家政策提案、读取、精确 action 判断与覆盖统计实现。"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from domains.compliance.errors import (
    CountryPolicyActivationConflictError,
    CountryPolicyApprovalFactInvalidError,
    CountryPolicyBaseVersionConflictError,
    CountryPolicyIdempotencyConflictError,
    CountryPolicyNotConfiguredError,
)
from domains.compliance.models import CountryPolicyActivation, CountryPolicyVersion
from domains.compliance.permissions import (
    ComplianceAction,
    ComplianceActor,
    ComplianceAuthorizer,
    ComplianceScope,
)
from domains.compliance.repository import ComplianceUnitOfWorkFactory
from domains.compliance.schemas import (
    CountryPolicyAction,
    CountryPolicyActivationView,
    CountryPolicyApprovalFact,
    CountryPolicyChangeSnapshot,
    CountryPolicyCoverage,
    CountryPolicyDecision,
    CountryPolicyProposalCreate,
    CountryPolicyProposalResult,
    CountryPolicyVersionView,
    normalize_country_key,
)
from shared.errors import TenantIsolationViolation, TransientError, ValidationError
from shared.events.catalog import CountryPolicyVersionProposed
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
    new_id,
)

_IDEMPOTENCY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_MAX_LIST_LIMIT = 200
_COUNTRY_POLICY_APPROVAL_TYPE = "country_policy_change"


def _utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError("合规服务时间必须是 UTC")
    return value.astimezone(UTC)


def _country_key(country: str) -> str:
    if not isinstance(country, str):
        raise ValidationError("国家键必须是字符串")
    try:
        return normalize_country_key(country)
    except ValueError as exc:
        raise ValidationError("国家键无效") from exc


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
        raise ValidationError("国家政策列表 limit 必须在 1 到 200 之间")
    return value


def _action(value: CountryPolicyAction) -> CountryPolicyAction:
    if not isinstance(value, CountryPolicyAction):
        raise ValidationError("国家政策 action 无效")
    return value


def _version_fact(
    value: object,
    tenant_id: TenantId,
    *,
    message: str,
) -> CountryPolicyVersion:
    if not isinstance(value, CountryPolicyVersion):
        raise TransientError(message)
    if value.tenant_id != tenant_id:
        raise TenantIsolationViolation("国家政策版本租户不匹配")
    return value


def _activation_fact(
    value: object,
    tenant_id: TenantId,
    *,
    message: str,
) -> CountryPolicyActivation:
    if not isinstance(value, CountryPolicyActivation):
        raise TransientError(message)
    if value.tenant_id != tenant_id:
        raise TenantIsolationViolation("国家政策激活事实租户不匹配")
    return value


def _proposal_result(version: CountryPolicyVersion) -> CountryPolicyProposalResult:
    return CountryPolicyProposalResult(
        country_policy_version_id=version.country_policy_version_id,
        country_key=version.country_key,
        version_number=version.version_number,
        content_hash=version.content_hash,
        change_set_ref=version.change_set_ref,
    )


def _idempotency_hit(
    value: object,
    tenant_id: TenantId,
    idempotency_key: IdempotencyKey,
    requested_hash: str,
) -> CountryPolicyProposalResult:
    version = _version_fact(
        value,
        tenant_id,
        message="国家政策幂等查询返回类型无效",
    )
    if version.idempotency_key != idempotency_key:
        raise TransientError("国家政策幂等查询返回的幂等键不匹配")
    if version.content_hash != requested_hash:
        raise CountryPolicyIdempotencyConflictError("国家政策幂等键对应不同内容")
    return _proposal_result(version)


def _approval_time(approval: object) -> tuple[CountryPolicyApprovalFact, datetime]:
    if not isinstance(approval, CountryPolicyApprovalFact):
        raise CountryPolicyApprovalFactInvalidError("国家政策审批事实类型无效")
    try:
        approved_at = _utc(approval.decided_at)
    except ValidationError as exc:
        raise CountryPolicyApprovalFactInvalidError(
            "国家政策审批时间必须是 UTC"
        ) from exc
    if (
        not isinstance(approval.approval_id, str)
        or not approval.approval_id.startswith("apr_")
        or not 4 < len(approval.approval_id) <= 40
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in approval.approval_id
        )
        or not isinstance(approval.decided_by, str)
        or not approval.decided_by.strip()
        or approval.decided_by != approval.decided_by.strip()
        or len(approval.decided_by) > 40
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in approval.decided_by
        )
    ):
        raise CountryPolicyApprovalFactInvalidError("国家政策审批身份事实无效")
    return approval, approved_at


def _require_approval_matches(
    candidate: CountryPolicyVersion,
    approval: CountryPolicyApprovalFact,
) -> None:
    if (
        approval.approval_type != _COUNTRY_POLICY_APPROVAL_TYPE
        or approval.change_set_ref != candidate.change_set_ref
        or approval.decided_by == candidate.proposed_by
    ):
        raise CountryPolicyApprovalFactInvalidError(
            "国家政策审批事实与精确候选版本不匹配"
        )


def _keyed_activation(
    value: object,
    tenant_id: TenantId,
    *,
    approval_id: ApprovalId | None = None,
    version_id: CountryPolicyVersionId | None = None,
) -> CountryPolicyActivation:
    activation = _activation_fact(
        value,
        tenant_id,
        message="国家政策激活键查询返回类型无效",
    )
    if approval_id is not None and activation.approval_id != approval_id:
        raise TransientError("国家政策激活审批键查询返回不匹配")
    if version_id is not None and activation.country_policy_version_id != version_id:
        raise TransientError("国家政策激活版本键查询返回不匹配")
    return activation


def _exact_replay(
    existing_by_approval: CountryPolicyActivation | None,
    existing_by_version: CountryPolicyActivation | None,
    candidate: CountryPolicyVersion,
    approval: CountryPolicyApprovalFact,
) -> CountryPolicyActivationView:
    if (
        existing_by_approval is None
        or existing_by_version is None
        or existing_by_approval != existing_by_version
    ):
        raise CountryPolicyActivationConflictError(
            "国家政策审批或版本已绑定其他激活事实"
        )
    existing = existing_by_approval
    if (
        existing.country_key != candidate.country_key
        or existing.country_policy_version_id != candidate.country_policy_version_id
        or existing.content_hash != candidate.content_hash
        or existing.approval_id != approval.approval_id
        or existing.change_set_ref != approval.change_set_ref
        or existing.approved_by != approval.decided_by
        or existing.approved_at != approval.decided_at
    ):
        raise CountryPolicyActivationConflictError("国家政策激活重放事实不一致")
    _require_approval_matches(candidate, approval)
    return existing.to_view()


def _require_base_matches(
    candidate: CountryPolicyVersion,
    current: CountryPolicyActivation | None,
) -> None:
    if candidate.base_version_id is None:
        matches = current is None
    else:
        matches = (
            current is not None
            and current.country_policy_version_id == candidate.base_version_id
            and current.content_hash == candidate.base_content_hash
        )
    if not matches:
        raise CountryPolicyBaseVersionConflictError(
            "国家政策候选基准已不是该国家当前版本"
        )


class ComplianceServiceImpl:
    """在 tenant-bound UoW 中组合不可变政策事实，不生成法律默认值。"""

    def __init__(
        self,
        uow_factory: ComplianceUnitOfWorkFactory,
        authorizer: ComplianceAuthorizer,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(uow_factory, ComplianceUnitOfWorkFactory):
            raise ValidationError("合规事务依赖无效")
        if not isinstance(authorizer, ComplianceAuthorizer):
            raise ValidationError("合规授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self,
        tenant_id: TenantId,
        actor: ComplianceActor,
        action: ComplianceAction,
        scope: ComplianceScope,
    ) -> None:
        self._authorizer.require(actor, action, scope, tenant_id)

    def _clock(self) -> datetime:
        return _utc(self._now())

    @staticmethod
    async def _resolve_activation(
        tenant_id: TenantId,
        country_key: str,
        activation: CountryPolicyActivation,
        versions: object,
    ) -> CountryPolicyVersion:
        if activation.country_key != country_key:
            raise TransientError("国家政策激活事实返回了错误国家键")
        get = getattr(versions, "get", None)
        if not callable(get):
            raise TransientError("国家政策版本仓储返回类型无效")
        raw_version = await get(tenant_id, activation.country_policy_version_id)
        if raw_version is None:
            raise TransientError("国家政策激活事实引用的版本不存在")
        version = _version_fact(
            raw_version,
            tenant_id,
            message="国家政策版本仓储返回类型无效",
        )
        if (
            version.country_policy_version_id != activation.country_policy_version_id
            or version.country_key != activation.country_key
            or version.content_hash != activation.content_hash
        ):
            raise TransientError("国家政策激活事实与版本不一致")
        return version

    async def get_active_policy(
        self, tenant_id: TenantId, country: str, *, actor: ComplianceActor
    ) -> CountryPolicyVersionView:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.TENANT,
        )
        country_key = _country_key(country)
        async with self._uow_factory(tenant_id) as uow:
            raw_activation = await uow.activations.get_current(tenant_id, country_key)
            if raw_activation is None:
                raise CountryPolicyNotConfiguredError("精确国家键没有已激活政策")
            activation = _activation_fact(
                raw_activation,
                tenant_id,
                message="国家政策激活仓储返回类型无效",
            )
            version = await self._resolve_activation(
                tenant_id, country_key, activation, uow.versions
            )
            return version.to_view()

    async def get_version(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyVersionView:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.TENANT,
        )
        if not isinstance(version_id, str) or not version_id.startswith("cpp_"):
            raise ValidationError("国家政策版本 ID 无效")
        async with self._uow_factory(tenant_id) as uow:
            raw_version = await uow.versions.get(tenant_id, version_id)
            if raw_version is None:
                raise ValidationError("国家政策版本不存在")
            version = _version_fact(
                raw_version,
                tenant_id,
                message="国家政策版本仓储返回类型无效",
            )
            if version.country_policy_version_id != version_id:
                raise TransientError("国家政策版本 ID 不匹配")
            return version.to_view()

    async def list_active_policies(
        self,
        tenant_id: TenantId,
        *,
        actor: ComplianceActor,
        limit: int = 50,
    ) -> list[CountryPolicyVersionView]:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.TENANT,
        )
        checked_limit = _limit(limit)
        async with self._uow_factory(tenant_id) as uow:
            raw_activations = await uow.activations.list_current(
                tenant_id, checked_limit
            )
            if not isinstance(raw_activations, list):
                raise TransientError("国家政策激活列表仓储返回类型无效")
            if len(raw_activations) > checked_limit:
                raise TransientError("国家政策激活列表超过请求上限")
            views: list[CountryPolicyVersionView] = []
            seen: set[str] = set()
            for raw_activation in raw_activations:
                activation = _activation_fact(
                    raw_activation,
                    tenant_id,
                    message="国家政策激活列表仓储返回类型无效",
                )
                if activation.country_key in seen:
                    raise TransientError("国家政策激活列表包含重复国家键")
                seen.add(activation.country_key)
                version = await self._resolve_activation(
                    tenant_id,
                    activation.country_key,
                    activation,
                    uow.versions,
                )
                views.append(version.to_view())
            return views

    async def list_versions(
        self,
        tenant_id: TenantId,
        country: str,
        *,
        actor: ComplianceActor,
        limit: int = 50,
    ) -> list[CountryPolicyVersionView]:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.TENANT,
        )
        country_key = _country_key(country)
        checked_limit = _limit(limit)
        async with self._uow_factory(tenant_id) as uow:
            raw_versions = await uow.versions.list(
                tenant_id, country_key, checked_limit
            )
            if not isinstance(raw_versions, list):
                raise TransientError("国家政策版本列表仓储返回类型无效")
            if len(raw_versions) > checked_limit:
                raise TransientError("国家政策版本列表超过请求上限")
            views: list[CountryPolicyVersionView] = []
            for raw_version in raw_versions:
                version = _version_fact(
                    raw_version,
                    tenant_id,
                    message="国家政策版本列表仓储返回类型无效",
                )
                if version.country_key != country_key:
                    raise TransientError("国家政策版本列表返回了错误国家键")
                views.append(version.to_view())
            return views

    async def get_country_policy_decision(
        self,
        tenant_id: TenantId,
        country: str,
        action: CountryPolicyAction,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyDecision:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_DECIDE,
            ComplianceScope.SYSTEM,
        )
        country_key = _country_key(country)
        checked_action = _action(action)
        async with self._uow_factory(tenant_id) as uow:
            raw_activation = await uow.activations.get_current(tenant_id, country_key)
            if raw_activation is None:
                return CountryPolicyDecision(
                    country_key=country_key,
                    action=checked_action,
                    configured=False,
                    allowed=False,
                    active_version_id=None,
                    content_hash=None,
                    requirements=(),
                )
            activation = _activation_fact(
                raw_activation,
                tenant_id,
                message="国家政策激活仓储返回类型无效",
            )
            version = await self._resolve_activation(
                tenant_id, country_key, activation, uow.versions
            )
            allowed = {
                CountryPolicyAction.PUBLIC_RESEARCH: version.public_research_allowed,
                CountryPolicyAction.CONTACT_ENRICHMENT: (
                    version.contact_enrichment_allowed
                ),
                CountryPolicyAction.COLD_B2B_EMAIL: version.cold_b2b_email_allowed,
            }[checked_action]
            return CountryPolicyDecision(
                country_key=country_key,
                action=checked_action,
                configured=True,
                allowed=allowed,
                active_version_id=version.country_policy_version_id,
                content_hash=version.content_hash,
                requirements=tuple(sorted(version.requirements)),
            )

    async def get_coverage(
        self, tenant_id: TenantId, *, actor: ComplianceActor
    ) -> CountryPolicyCoverage:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_READ,
            ComplianceScope.TENANT,
        )
        async with self._uow_factory(tenant_id) as uow:
            raw_activations = await uow.activations.list_current(
                tenant_id, _MAX_LIST_LIMIT + 1
            )
            if not isinstance(raw_activations, list):
                raise TransientError("国家政策覆盖仓储返回类型无效")
            if len(raw_activations) > _MAX_LIST_LIMIT:
                raise TransientError("国家政策覆盖超过可验证上限")
            seen: set[str] = set()
            enrichment_count = 0
            for raw_activation in raw_activations:
                activation = _activation_fact(
                    raw_activation,
                    tenant_id,
                    message="国家政策覆盖仓储返回类型无效",
                )
                if activation.country_key in seen:
                    raise TransientError("国家政策覆盖包含重复国家键")
                seen.add(activation.country_key)
                version = await self._resolve_activation(
                    tenant_id,
                    activation.country_key,
                    activation,
                    uow.versions,
                )
                if version.contact_enrichment_allowed:
                    enrichment_count += 1
            return CountryPolicyCoverage(
                active_policy_count=len(raw_activations),
                contact_enrichment_allowed_count=enrichment_count,
            )

    async def get_change_snapshot(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyChangeSnapshot:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_CHANGE_SNAPSHOT_READ,
            ComplianceScope.SYSTEM,
        )
        if not isinstance(version_id, str) or not version_id.startswith("cpp_"):
            raise ValidationError("国家政策版本 ID 无效")
        async with self._uow_factory(tenant_id) as uow:
            raw_candidate = await uow.versions.get(tenant_id, version_id)
            if raw_candidate is None:
                raise ValidationError("国家政策候选版本不存在")
            candidate = _version_fact(
                raw_candidate,
                tenant_id,
                message="国家政策候选仓储返回类型无效",
            )
            if candidate.country_policy_version_id != version_id:
                raise TransientError("国家政策候选版本 ID 不匹配")
            base: CountryPolicyVersion | None = None
            if candidate.base_version_id is not None:
                raw_base = await uow.versions.get(tenant_id, candidate.base_version_id)
                if raw_base is None:
                    raise TransientError("国家政策候选引用的基准版本不存在")
                base = _version_fact(
                    raw_base,
                    tenant_id,
                    message="国家政策基准仓储返回类型无效",
                )
                if base.country_policy_version_id != candidate.base_version_id:
                    raise TransientError("国家政策基准版本 ID 不匹配")
                if (
                    base.country_key != candidate.country_key
                    or base.content_hash != candidate.base_content_hash
                ):
                    raise TransientError("国家政策候选引用的基准版本不一致")
            raw_current = await uow.activations.get_current(
                tenant_id, candidate.country_key
            )
            current: CountryPolicyVersion | None = None
            if raw_current is not None:
                activation = _activation_fact(
                    raw_current,
                    tenant_id,
                    message="国家政策当前激活仓储返回类型无效",
                )
                current = await self._resolve_activation(
                    tenant_id,
                    candidate.country_key,
                    activation,
                    uow.versions,
                )
            base_is_current = (base is None and current is None) or (
                base is not None
                and current is not None
                and base.country_policy_version_id == current.country_policy_version_id
                and base.content_hash == current.content_hash
            )
            return CountryPolicyChangeSnapshot(
                base=None if base is None else base.to_view(),
                current=None if current is None else current.to_view(),
                candidate=candidate.to_view(),
                base_is_current=base_is_current,
            )

    async def propose_country_policy(
        self,
        tenant_id: TenantId,
        command: CountryPolicyProposalCreate,
        *,
        actor: ComplianceActor,
        idempotency_key: IdempotencyKey,
    ) -> CountryPolicyProposalResult:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_PROPOSE,
            ComplianceScope.TENANT,
        )
        if not isinstance(command, CountryPolicyProposalCreate):
            raise ValidationError("国家政策提交内容无效")
        if (
            not isinstance(idempotency_key, str)
            or _IDEMPOTENCY_PATTERN.fullmatch(idempotency_key) is None
        ):
            raise ValidationError("国家政策幂等键格式无效")
        country_key = _country_key(command.country)
        requested_hash = CountryPolicyVersion.content_hash_for(command)
        async with self._uow_factory(tenant_id) as uow:
            existing = await uow.versions.find_by_idempotency_key(
                tenant_id, idempotency_key
            )
            if existing is not None:
                return _idempotency_hit(
                    existing,
                    tenant_id,
                    idempotency_key,
                    requested_hash,
                )

            await uow.versions.lock_idempotency_key(tenant_id, idempotency_key)
            existing = await uow.versions.find_by_idempotency_key(
                tenant_id, idempotency_key
            )
            if existing is not None:
                return _idempotency_hit(
                    existing,
                    tenant_id,
                    idempotency_key,
                    requested_hash,
                )
            await uow.versions.lock_country(tenant_id, country_key)
            existing = await uow.versions.find_by_idempotency_key(
                tenant_id, idempotency_key
            )
            if existing is not None:
                return _idempotency_hit(
                    existing,
                    tenant_id,
                    idempotency_key,
                    requested_hash,
                )

            raw_current = await uow.activations.get_current(tenant_id, country_key)
            current: CountryPolicyActivation | None = None
            if raw_current is not None:
                current = _activation_fact(
                    raw_current,
                    tenant_id,
                    message="国家政策当前激活仓储返回类型无效",
                )
                await self._resolve_activation(
                    tenant_id, country_key, current, uow.versions
                )
            version_number = await uow.versions.next_version_number(
                tenant_id, country_key
            )
            if (
                isinstance(version_number, bool)
                or not isinstance(version_number, int)
                or version_number < 1
            ):
                raise TransientError("国家政策版本号仓储返回类型无效")
            proposed_at = self._clock()
            version = CountryPolicyVersion.from_command(
                tenant_id=tenant_id,
                version_id=CountryPolicyVersionId(new_id("cpp")),
                version_number=version_number,
                command=command,
                base_version_id=(
                    None if current is None else current.country_policy_version_id
                ),
                base_content_hash=None if current is None else current.content_hash,
                proposed_by=EmployeeId(actor.actor_id),
                proposed_at=proposed_at,
                idempotency_key=idempotency_key,
            )
            await uow.versions.add(tenant_id, version)
            await uow.provenance.add_for_version(
                tenant_id,
                version.country_policy_version_id,
                version.field_provenance,
            )
            await uow.bus.publish(
                CountryPolicyVersionProposed(
                    tenant_id=tenant_id,
                    occurred_at=proposed_at,
                    run_id=None,
                    country_policy_version_id=version.country_policy_version_id,
                    country_key=version.country_key,
                    content_hash=version.content_hash,
                    proposed_by=version.proposed_by,
                )
            )
            return _proposal_result(version)

    async def activate_country_policy(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        approval: CountryPolicyApprovalFact,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyActivationView:
        self._require(
            tenant_id,
            actor,
            ComplianceAction.COUNTRY_POLICY_ACTIVATE,
            ComplianceScope.SYSTEM,
        )
        checked_approval, approved_at = _approval_time(approval)
        if not isinstance(version_id, str) or not version_id.startswith("cpp_"):
            raise CountryPolicyApprovalFactInvalidError("国家政策候选版本 ID 无效")
        async with self._uow_factory(tenant_id) as uow:
            raw_candidate = await uow.versions.get(tenant_id, version_id)
            if raw_candidate is None:
                raise CountryPolicyApprovalFactInvalidError("国家政策候选版本不存在")
            discovered = _version_fact(
                raw_candidate,
                tenant_id,
                message="国家政策候选仓储返回类型无效",
            )
            if discovered.country_policy_version_id != version_id:
                raise TransientError("国家政策候选版本 ID 不匹配")

            await uow.versions.lock_country(tenant_id, discovered.country_key)
            raw_locked_candidate = await uow.versions.get(tenant_id, version_id)
            if raw_locked_candidate is None:
                raise TransientError("国家政策候选在国家锁内消失")
            candidate = _version_fact(
                raw_locked_candidate,
                tenant_id,
                message="国家政策候选仓储返回类型无效",
            )
            if candidate != discovered:
                raise TransientError("国家政策候选在国家锁内返回不一致")

            raw_by_approval = await uow.activations.get_by_approval(
                tenant_id, checked_approval.approval_id
            )
            existing_by_approval = (
                None
                if raw_by_approval is None
                else _keyed_activation(
                    raw_by_approval,
                    tenant_id,
                    approval_id=checked_approval.approval_id,
                )
            )
            raw_by_version = await uow.activations.get_by_version(
                tenant_id, candidate.country_policy_version_id
            )
            existing_by_version = (
                None
                if raw_by_version is None
                else _keyed_activation(
                    raw_by_version,
                    tenant_id,
                    version_id=candidate.country_policy_version_id,
                )
            )

            activated_at = self._clock()
            if approved_at > activated_at:
                raise CountryPolicyApprovalFactInvalidError(
                    "国家政策审批时间晚于激活时间"
                )
            if existing_by_approval is not None or existing_by_version is not None:
                return _exact_replay(
                    existing_by_approval,
                    existing_by_version,
                    candidate,
                    checked_approval,
                )

            _require_approval_matches(candidate, checked_approval)
            raw_current = await uow.activations.get_current(
                tenant_id, candidate.country_key
            )
            current: CountryPolicyActivation | None = None
            if raw_current is not None:
                current = _activation_fact(
                    raw_current,
                    tenant_id,
                    message="国家政策当前激活仓储返回类型无效",
                )
                await self._resolve_activation(
                    tenant_id,
                    candidate.country_key,
                    current,
                    uow.versions,
                )
            _require_base_matches(candidate, current)
            activation_sequence = await uow.activations.next_activation_sequence(
                tenant_id, candidate.country_key
            )
            if (
                isinstance(activation_sequence, bool)
                or not isinstance(activation_sequence, int)
                or activation_sequence < 1
            ):
                raise TransientError("国家政策激活序号仓储返回类型无效")
            activation = CountryPolicyActivation(
                tenant_id=tenant_id,
                activation_id=CountryPolicyActivationId(new_id("cpa")),
                activation_sequence=activation_sequence,
                country_key=candidate.country_key,
                country_policy_version_id=candidate.country_policy_version_id,
                content_hash=candidate.content_hash,
                approval_id=checked_approval.approval_id,
                change_set_ref=checked_approval.change_set_ref,
                approved_by=checked_approval.decided_by,
                approved_at=approved_at,
                activated_by=actor.actor_id,
                activated_at=activated_at,
            )
            await uow.activations.add(tenant_id, activation)
            return activation.to_view()


__all__ = ("ComplianceServiceImpl",)
