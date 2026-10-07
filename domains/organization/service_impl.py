"""Company Playbook 提案、读取与精确审批激活实现。"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from domains.organization.errors import (
    PlaybookActivationConflictError,
    PlaybookApprovalFactInvalidError,
    PlaybookBaseVersionConflictError,
    PlaybookIdempotencyConflictError,
    PlaybookNotConfiguredError,
)
from domains.organization.models import (
    CompanyPlaybook,
    CompanyPlaybookVersion,
    PlaybookActivation,
)
from domains.organization.permissions import (
    OrganizationAction,
    OrganizationActor,
    OrganizationAuthorizer,
)
from domains.organization.repository import OrganizationUnitOfWorkFactory
from domains.organization.schemas import (
    PlaybookActivationView,
    PlaybookApprovalFact,
    PlaybookChangeSnapshot,
    PlaybookProposalCreate,
    PlaybookProposalResult,
    PlaybookVersionView,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    IdempotencyKey,
    PlaybookActivationId,
    PlaybookVersionId,
    TenantId,
    new_id,
)

_IDEMPOTENCY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


def _utc(value: datetime, message: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError(message)
    return value.astimezone(UTC)


def _proposal_result(version: CompanyPlaybookVersion) -> PlaybookProposalResult:
    return PlaybookProposalResult(
        playbook_version_id=version.playbook_version_id,
        version_number=version.version_number,
        content_hash=version.content_hash,
        change_set_ref=version.change_set_ref,
    )


def _require_approval_matches(
    version: CompanyPlaybookVersion,
    approval: PlaybookApprovalFact,
) -> None:
    if (
        approval.approval_type != "playbook_change"
        or approval.change_set_ref != version.change_set_ref
    ):
        raise PlaybookApprovalFactInvalidError("Playbook 审批事实与候选版本不匹配")


def _require_exact_replay(
    existing_by_approval: PlaybookActivation | None,
    existing_by_version: PlaybookActivation | None,
    version: CompanyPlaybookVersion,
    approval: PlaybookApprovalFact,
) -> PlaybookActivationView:
    if (
        existing_by_approval is None
        or existing_by_version is None
        or existing_by_approval != existing_by_version
    ):
        raise PlaybookActivationConflictError("Playbook 审批或版本已绑定其他激活事实")
    existing = existing_by_approval
    if (
        existing.playbook_version_id != version.playbook_version_id
        or existing.content_hash != version.content_hash
        or existing.approval_id != approval.approval_id
        or existing.change_set_ref != approval.change_set_ref
        or existing.approved_by != approval.decided_by
        or existing.approved_at != approval.decided_at
    ):
        raise PlaybookActivationConflictError("Playbook 激活重放事实不一致")
    _require_approval_matches(version, approval)
    return existing.to_view()


def _require_base_matches(
    version: CompanyPlaybookVersion,
    current: PlaybookActivation | None,
) -> None:
    if version.base_version_id is None:
        matches = current is None
    else:
        matches = (
            current is not None
            and current.playbook_version_id == version.base_version_id
            and current.content_hash == version.base_content_hash
        )
    if not matches:
        raise PlaybookBaseVersionConflictError("Playbook 候选基准已不是当前版本")


class OrganizationServiceImpl:
    """全部读取/写入先授权，跨表事实在 tenant-bound UoW 内组合。"""

    def __init__(
        self,
        uow_factory: OrganizationUnitOfWorkFactory,
        authorizer: OrganizationAuthorizer,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(uow_factory, OrganizationUnitOfWorkFactory):
            raise ValidationError("组织事务依赖无效")
        if not isinstance(authorizer, OrganizationAuthorizer):
            raise ValidationError("组织授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self,
        tenant_id: TenantId,
        actor: OrganizationActor,
        action: OrganizationAction,
    ) -> None:
        self._authorizer.require(actor, action, tenant_id)

    def _clock(self) -> datetime:
        return _utc(self._now(), "组织服务时间必须是 UTC")

    async def get_playbook(
        self, tenant_id: TenantId, *, actor: OrganizationActor
    ) -> CompanyPlaybook:
        self._require(tenant_id, actor, OrganizationAction.PLAYBOOK_READ)
        async with self._uow_factory(tenant_id) as uow:
            activation = await uow.activations.get_current(tenant_id)
            if activation is None:
                raise PlaybookNotConfiguredError("Company Playbook 尚未配置")
            version = await uow.versions.get(
                tenant_id, activation.playbook_version_id
            )
            if version is None:
                raise TransientError("Playbook 激活事实引用的版本不存在")
            return CompanyPlaybook.from_facts(version, activation)

    async def get_version(
        self,
        tenant_id: TenantId,
        version_id: PlaybookVersionId,
        *,
        actor: OrganizationActor,
    ) -> PlaybookVersionView:
        self._require(tenant_id, actor, OrganizationAction.PLAYBOOK_READ)
        async with self._uow_factory(tenant_id) as uow:
            version = await uow.versions.get(tenant_id, version_id)
            if version is None:
                raise ValidationError("Playbook 版本不存在")
            return version.to_view()

    async def list_versions(
        self,
        tenant_id: TenantId,
        *,
        actor: OrganizationActor,
        limit: int = 50,
    ) -> list[PlaybookVersionView]:
        self._require(tenant_id, actor, OrganizationAction.PLAYBOOK_READ)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("Playbook 版本列表 limit 必须在 1 到 200 之间")
        async with self._uow_factory(tenant_id) as uow:
            versions = await uow.versions.list(tenant_id, limit)
            return [version.to_view() for version in versions]

    async def get_change_snapshot(
        self,
        tenant_id: TenantId,
        version_id: PlaybookVersionId,
        *,
        actor: OrganizationActor,
    ) -> PlaybookChangeSnapshot:
        self._require(
            tenant_id,
            actor,
            OrganizationAction.PLAYBOOK_CHANGE_SNAPSHOT_READ,
        )
        async with self._uow_factory(tenant_id) as uow:
            candidate = await uow.versions.get(tenant_id, version_id)
            if candidate is None:
                raise ValidationError("Playbook 候选版本不存在")
            base = None
            if candidate.base_version_id is not None:
                base = await uow.versions.get(
                    tenant_id, candidate.base_version_id
                )
                if base is None or base.content_hash != candidate.base_content_hash:
                    raise TransientError("Playbook 候选引用的基准版本不完整")
            current_activation = await uow.activations.get_current(tenant_id)
            current = None
            if current_activation is not None:
                current = await uow.versions.get(
                    tenant_id, current_activation.playbook_version_id
                )
                if (
                    current is None
                    or current.content_hash != current_activation.content_hash
                ):
                    raise TransientError("Playbook 当前激活事实引用不完整")
            base_is_current = (
                base is None and current is None
            ) or (
                base is not None
                and current is not None
                and base.playbook_version_id == current.playbook_version_id
                and base.content_hash == current.content_hash
            )
            return PlaybookChangeSnapshot(
                base=None if base is None else base.to_view(),
                current=None if current is None else current.to_view(),
                candidate=candidate.to_view(),
                base_is_current=base_is_current,
            )

    async def propose_playbook(
        self,
        tenant_id: TenantId,
        command: PlaybookProposalCreate,
        *,
        actor: OrganizationActor,
        idempotency_key: IdempotencyKey,
    ) -> PlaybookProposalResult:
        self._require(tenant_id, actor, OrganizationAction.PLAYBOOK_PROPOSE)
        if not isinstance(command, PlaybookProposalCreate):
            raise ValidationError("Playbook 提交内容无效")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_PATTERN.fullmatch(
            idempotency_key
        ):
            raise ValidationError("Playbook 幂等键格式无效")
        requested_hash = CompanyPlaybookVersion.content_hash_for(command)
        async with self._uow_factory(tenant_id) as uow:
            existing = await uow.versions.find_by_idempotency_key(
                tenant_id, idempotency_key
            )
            if existing is not None:
                if existing.content_hash != requested_hash:
                    raise PlaybookIdempotencyConflictError(
                        "Playbook 幂等键对应不同内容"
                    )
                return _proposal_result(existing)
            await uow.activations.lock_tenant(tenant_id)
            existing = await uow.versions.find_by_idempotency_key(
                tenant_id, idempotency_key
            )
            if existing is not None:
                if existing.content_hash != requested_hash:
                    raise PlaybookIdempotencyConflictError(
                        "Playbook 幂等键对应不同内容"
                    )
                return _proposal_result(existing)
            current = await uow.activations.get_current(tenant_id)
            version = CompanyPlaybookVersion.from_command(
                tenant_id=tenant_id,
                version_id=PlaybookVersionId(new_id("pbv")),
                version_number=await uow.versions.next_version_number(tenant_id),
                command=command,
                base_version_id=(
                    None if current is None else current.playbook_version_id
                ),
                base_content_hash=None if current is None else current.content_hash,
                proposed_by=EmployeeId(actor.actor_id),
                proposed_at=self._clock(),
                idempotency_key=idempotency_key,
            )
            await uow.versions.add(version)
            return _proposal_result(version)

    async def activate_playbook(
        self,
        tenant_id: TenantId,
        version_id: PlaybookVersionId,
        approval: PlaybookApprovalFact,
        *,
        actor: OrganizationActor,
    ) -> PlaybookActivationView:
        self._require(tenant_id, actor, OrganizationAction.PLAYBOOK_ACTIVATE)
        if not isinstance(approval, PlaybookApprovalFact):
            raise PlaybookApprovalFactInvalidError("Playbook 审批事实类型无效")
        try:
            approved_at = _utc(
                approval.decided_at, "Playbook 审批时间必须是 UTC"
            )
        except ValidationError as exc:
            raise PlaybookApprovalFactInvalidError(
                "Playbook 审批时间必须是 UTC"
            ) from exc
        activated_at = self._clock()
        if approved_at > activated_at:
            raise PlaybookApprovalFactInvalidError("Playbook 审批时间晚于激活时间")
        async with self._uow_factory(tenant_id) as uow:
            await uow.activations.lock_tenant(tenant_id)
            version = await uow.versions.get(tenant_id, version_id)
            if version is None:
                raise PlaybookApprovalFactInvalidError("Playbook 候选版本不存在")
            existing_by_approval = await uow.activations.get_by_approval(
                tenant_id, approval.approval_id
            )
            existing_by_version = await uow.activations.get_by_version(
                tenant_id, version_id
            )
            if existing_by_approval is not None or existing_by_version is not None:
                return _require_exact_replay(
                    existing_by_approval,
                    existing_by_version,
                    version,
                    approval,
                )
            _require_approval_matches(version, approval)
            current = await uow.activations.get_current(tenant_id)
            _require_base_matches(version, current)
            activation = PlaybookActivation(
                tenant_id=tenant_id,
                activation_id=PlaybookActivationId(new_id("pba")),
                playbook_version_id=version.playbook_version_id,
                content_hash=version.content_hash,
                approval_id=approval.approval_id,
                change_set_ref=approval.change_set_ref,
                approved_by=approval.decided_by,
                approved_at=approved_at,
                activated_by=actor.actor_id,
                activated_at=activated_at,
            )
            await uow.activations.add(activation)
            return activation.to_view()


__all__ = ("OrganizationServiceImpl",)
