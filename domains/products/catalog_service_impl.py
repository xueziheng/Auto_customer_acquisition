"""Catalog Product Proposal 策略版本生命周期的 tenant-bound 实现。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from pydantic import ValidationError as PydanticValidationError

from domains.products.catalog_rules import catalog_policy_content_hash
from domains.products.errors import (
    CatalogPolicyApprovalConflictError,
    CatalogPolicyDecisionInvalidError,
    CatalogPolicyIdempotencyConflictError,
    CatalogPolicyNotFoundError,
    CatalogPolicyStateTransitionError,
)
from domains.products.models import (
    CatalogProposalPolicyState,
    CatalogProposalPolicyVersion,
)
from domains.products.permissions import (
    ProductAction,
    ProductActor,
    ProductAuthorizer,
)
from domains.products.repository import CatalogProductsUnitOfWork
from domains.products.schemas import (
    CatalogApprovalDecisionInput,
    CatalogPolicyChangeSnapshot,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
)
from domains.products.service import catalog_policy_creation_request_hash
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    TradeOSError,
    TransientError,
    ValidationError,
)
from shared.events.catalog import CatalogProposalPolicyActivated
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    TenantId,
    new_id,
)

_POLICY_UNAVAILABLE = "目录提案策略暂不可用"
_POLICY_NOT_FOUND = "目录提案策略不存在"
_CREATION_CONFLICT = "目录提案策略创建幂等键冲突"
_APPROVAL_CONFLICT = "目录提案策略审批绑定冲突"
_DECISION_INVALID = "目录提案策略审批事实不匹配"


def _clock(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError("目录提案策略服务时间必须是 UTC")
    return value.astimezone(UTC)


def _creation_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValidationError("目录提案策略幂等键无效")
    return value


def _policy_id(value: CatalogProposalPolicyVersionId) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("cpv_")
        or len(value) <= 4
        or len(value) > 40
        or value != value.strip()
    ):
        raise ValidationError("目录提案策略版本 ID 无效")


def _approval_id(value: ApprovalId) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 40
    ):
        raise ValidationError("目录提案策略审批 ID 无效")


def _request_hash(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError("目录提案策略请求摘要无效")


def _content(value: CatalogProposalPolicyContent) -> CatalogProposalPolicyContent:
    if not isinstance(value, CatalogProposalPolicyContent):
        raise ValidationError("目录提案策略内容无效")
    try:
        return CatalogProposalPolicyContent.model_validate(
            value.model_dump(mode="python")
        )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        raise ValidationError("目录提案策略内容无效") from None


def _decision(value: CatalogApprovalDecisionInput) -> CatalogApprovalDecisionInput:
    if not isinstance(value, CatalogApprovalDecisionInput):
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)
    try:
        return CatalogApprovalDecisionInput.model_validate(
            value.model_dump(mode="python")
        )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID) from None


def _view(policy: CatalogProposalPolicyVersion) -> CatalogProposalPolicyView:
    return CatalogProposalPolicyView(
        policy_version_id=policy.policy_version_id,
        content=policy.content,
        content_hash=policy.content_hash,
        base_active_version_id=policy.base_active_version_id,
        proposed_by=policy.proposed_by,
        approval_id=policy.approval_id,
        state=policy.state.value,
        created_at=policy.created_at,
        activated_at=policy.activated_at,
        terminal_at=policy.terminal_at,
    )


def _policy_fact(
    value: CatalogProposalPolicyVersion,
    tenant_id: TenantId,
    *,
    expected_id: CatalogProposalPolicyVersionId | None = None,
) -> CatalogProposalPolicyVersion:
    if (
        not isinstance(value, CatalogProposalPolicyVersion)
        or value.tenant_id != tenant_id
        or (expected_id is not None and value.policy_version_id != expected_id)
    ):
        raise TransientError(_POLICY_UNAVAILABLE)
    try:
        value.__post_init__()
    except Exception:  # noqa: BLE001 -- 损坏持久事实不得泄漏底层异常
        raise TransientError(_POLICY_UNAVAILABLE) from None
    return value


def _active_fact(
    value: CatalogProposalPolicyVersion | None, tenant_id: TenantId
) -> CatalogProposalPolicyVersion | None:
    if value is None:
        return None
    policy = _policy_fact(value, tenant_id)
    if policy.state is not CatalogProposalPolicyState.ACTIVE:
        raise TransientError(_POLICY_UNAVAILABLE)
    return policy


def _same_creation_request(
    existing: CatalogProposalPolicyVersion,
    content: CatalogProposalPolicyContent,
    proposed_by: EmployeeId,
) -> bool:
    return (
        existing.proposed_by == proposed_by
        and existing.content == content
        and existing.content_hash == catalog_policy_content_hash(content)
        and existing.creation_request_hash
        == catalog_policy_creation_request_hash(
            content, proposed_by, existing.base_active_version_id
        )
    )


def _base_matches(
    candidate: CatalogProposalPolicyVersion,
    current: CatalogProposalPolicyVersion | None,
) -> bool:
    if candidate.base_active_version_id is None:
        return current is None
    return (
        current is not None
        and current.policy_version_id == candidate.base_active_version_id
    )


def _require_exact_decision(
    candidate: CatalogProposalPolicyVersion,
    decision: CatalogApprovalDecisionInput,
    now: datetime,
) -> None:
    if (
        decision.approval_type != "catalog_proposal_policy_change"
        or decision.contract_namespace != "catalog-policy-v1"
        or decision.change_set_ref
        != f"catalog-policy:{candidate.policy_version_id}:{candidate.content_hash}"
        or decision.request_hash != candidate.creation_request_hash
        or decision.proposed_by_run is not None
        or decision.proposed_by_employee != candidate.proposed_by
        or decision.owner_employee != candidate.proposed_by
        or decision.approval_id != candidate.approval_id
    ):
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)
    if decision.state in {"approved", "rejected"}:
        if decision.decided_at is None or decision.decided_at > now:
            raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)
    elif decision.expires_at > now:
        raise CatalogPolicyDecisionInvalidError(_DECISION_INVALID)


def _raise_storage_error(error: Exception) -> NoReturn:
    if isinstance(
        error,
        (
            CatalogPolicyApprovalConflictError,
            CatalogPolicyDecisionInvalidError,
            CatalogPolicyIdempotencyConflictError,
            CatalogPolicyNotFoundError,
            CatalogPolicyStateTransitionError,
        ),
    ):
        raise error
    if isinstance(error, IdempotencyConflict):
        raise CatalogPolicyApprovalConflictError(_APPROVAL_CONFLICT) from None
    if isinstance(error, InvalidStateTransition):
        raise CatalogPolicyStateTransitionError("目录提案策略状态转换冲突") from None
    if isinstance(error, (ValidationError, TransientError)):
        raise error
    if isinstance(error, TradeOSError):
        raise TransientError(_POLICY_UNAVAILABLE) from None
    raise TransientError(_POLICY_UNAVAILABLE) from None


class CatalogProposalServiceImpl:
    """策略创建、读取与决定应用；所有写入及事件共享一个 Products UoW。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], CatalogProductsUnitOfWork],
        authorizer: ProductAuthorizer,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValidationError("目录提案策略事务依赖无效")
        if not isinstance(authorizer, ProductAuthorizer):
            raise ValidationError("目录提案策略授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self, tenant_id: TenantId, actor: ProductActor, action: ProductAction
    ) -> None:
        self._authorizer.require(actor, action, tenant_id)

    def _clock(self) -> datetime:
        return _clock(self._now())

    async def create_policy_candidate(
        self,
        tenant_id: TenantId,
        content: CatalogProposalPolicyContent,
        *,
        idempotency_key: str,
        actor: ProductActor,
    ) -> CatalogProposalPolicyVersionId:
        self._require(tenant_id, actor, ProductAction.CATALOG_POLICY_PROPOSE)
        checked_content = _content(content)
        checked_key = _creation_key(idempotency_key)
        proposed_by = EmployeeId(actor.actor_id)
        try:
            async with self._uow_factory(tenant_id) as uow:
                await uow.policies.lock_policy_namespace(tenant_id)
                existing = await uow.policies.get_by_creation_key(
                    tenant_id, proposed_by, checked_key
                )
                if existing is not None:
                    existing = _policy_fact(existing, tenant_id)
                    if not _same_creation_request(
                        existing, checked_content, proposed_by
                    ):
                        raise CatalogPolicyIdempotencyConflictError(_CREATION_CONFLICT)
                    return existing.policy_version_id

                current = _active_fact(
                    await uow.policies.get_active(tenant_id, for_update=True),
                    tenant_id,
                )
                existing = await uow.policies.get_by_creation_key(
                    tenant_id, proposed_by, checked_key
                )
                if existing is not None:
                    existing = _policy_fact(existing, tenant_id)
                    if not _same_creation_request(
                        existing, checked_content, proposed_by
                    ):
                        raise CatalogPolicyIdempotencyConflictError(_CREATION_CONFLICT)
                    return existing.policy_version_id

                base_id = None if current is None else current.policy_version_id
                candidate = CatalogProposalPolicyVersion(
                    tenant_id=tenant_id,
                    policy_version_id=CatalogProposalPolicyVersionId(new_id("cpv")),
                    content=checked_content,
                    content_hash=catalog_policy_content_hash(checked_content),
                    base_active_version_id=base_id,
                    proposed_by=proposed_by,
                    creation_key=checked_key,
                    creation_request_hash=catalog_policy_creation_request_hash(
                        checked_content, proposed_by, base_id
                    ),
                    approval_id=None,
                    state=CatalogProposalPolicyState.PENDING_APPROVAL,
                    created_at=self._clock(),
                )
                stored = _policy_fact(
                    await uow.policies.add(tenant_id, candidate), tenant_id
                )
                if not _same_creation_request(stored, checked_content, proposed_by):
                    raise CatalogPolicyIdempotencyConflictError(_CREATION_CONFLICT)
                return stored.policy_version_id
        except Exception as error:
            if isinstance(error, CatalogPolicyIdempotencyConflictError):
                raise
            if isinstance(error, IdempotencyConflict):
                raise CatalogPolicyIdempotencyConflictError(
                    _CREATION_CONFLICT
                ) from None
            _raise_storage_error(error)

    async def get_active_policy(
        self, tenant_id: TenantId, *, actor: ProductActor
    ) -> CatalogProposalPolicyView | None:
        self._require(tenant_id, actor, ProductAction.CATALOG_POLICY_READ)
        try:
            async with self._uow_factory(tenant_id) as uow:
                active = _active_fact(
                    await uow.policies.get_active(tenant_id), tenant_id
                )
                return None if active is None else _view(active)
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def list_policy_versions(
        self, tenant_id: TenantId, *, actor: ProductActor, limit: int
    ) -> tuple[CatalogProposalPolicyView, ...]:
        self._require(tenant_id, actor, ProductAction.CATALOG_POLICY_READ)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValidationError("目录提案策略历史 limit 必须为 1..200")
        try:
            async with self._uow_factory(tenant_id) as uow:
                page = await uow.policies.list_versions(tenant_id, limit=limit)
                if len(page.items) > limit:
                    raise TransientError(_POLICY_UNAVAILABLE)
                return tuple(
                    _view(_policy_fact(item, tenant_id)) for item in page.items
                )
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def get_policy_change_snapshot(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        *,
        actor: ProductActor,
    ) -> CatalogPolicyChangeSnapshot:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        _policy_id(policy_version_id)
        try:
            async with self._uow_factory(tenant_id) as uow:
                candidate = await uow.policies.get(tenant_id, policy_version_id)
                if candidate is None:
                    raise CatalogPolicyNotFoundError(_POLICY_NOT_FOUND)
                candidate = _policy_fact(
                    candidate, tenant_id, expected_id=policy_version_id
                )
                base = None
                if candidate.base_active_version_id is not None:
                    base = await uow.policies.get(
                        tenant_id, candidate.base_active_version_id
                    )
                    if base is None:
                        raise TransientError(_POLICY_UNAVAILABLE)
                    base = _policy_fact(
                        base,
                        tenant_id,
                        expected_id=candidate.base_active_version_id,
                    )
                current = _active_fact(
                    await uow.policies.get_active(tenant_id), tenant_id
                )
                return CatalogPolicyChangeSnapshot(
                    base=None if base is None else _view(base),
                    current=None if current is None else _view(current),
                    candidate=_view(candidate),
                    base_is_current=_base_matches(candidate, current),
                )
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def bind_policy_approval(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        approval_id: ApprovalId,
        request_hash: str,
        *,
        actor: ProductActor,
    ) -> CatalogProposalPolicyView:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        _policy_id(policy_version_id)
        _approval_id(approval_id)
        _request_hash(request_hash)
        try:
            async with self._uow_factory(tenant_id) as uow:
                candidate = await uow.policies.bind_approval(
                    tenant_id,
                    policy_version_id,
                    approval_id,
                    request_hash,
                )
                if candidate is None:
                    raise CatalogPolicyNotFoundError(_POLICY_NOT_FOUND)
                candidate = _policy_fact(
                    candidate, tenant_id, expected_id=policy_version_id
                )
                if (
                    candidate.approval_id != approval_id
                    or candidate.creation_request_hash != request_hash
                ):
                    raise CatalogPolicyApprovalConflictError(_APPROVAL_CONFLICT)
                return _view(candidate)
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)

    async def apply_policy_decision(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        decision: CatalogApprovalDecisionInput,
        *,
        actor: ProductActor,
    ) -> CatalogProposalPolicyView:
        self._require(tenant_id, actor, ProductAction.CATALOG_SYSTEM_APPLY)
        _policy_id(policy_version_id)
        checked_decision = _decision(decision)
        applied_at = self._clock()
        try:
            async with self._uow_factory(tenant_id) as uow:
                await uow.policies.lock_policy_namespace(tenant_id)
                candidate = await uow.policies.get_for_update(
                    tenant_id, policy_version_id
                )
                if candidate is None:
                    raise CatalogPolicyNotFoundError(_POLICY_NOT_FOUND)
                candidate = _policy_fact(
                    candidate, tenant_id, expected_id=policy_version_id
                )
                _require_exact_decision(candidate, checked_decision, applied_at)
                matching_terminal = {
                    "approved": {
                        CatalogProposalPolicyState.ACTIVE,
                        CatalogProposalPolicyState.SUPERSEDED,
                        CatalogProposalPolicyState.STALE,
                    },
                    "rejected": {CatalogProposalPolicyState.REJECTED},
                    "expired": {CatalogProposalPolicyState.EXPIRED},
                }[checked_decision.state]
                if candidate.state in matching_terminal:
                    return _view(candidate)
                if candidate.state is not CatalogProposalPolicyState.PENDING_APPROVAL:
                    raise CatalogPolicyStateTransitionError(
                        "目录提案策略终态不允许应用另一决定"
                    )

                if checked_decision.state == "rejected":
                    return _view(
                        await uow.policies.update(
                            tenant_id,
                            replace(
                                candidate,
                                state=CatalogProposalPolicyState.REJECTED,
                                terminal_at=applied_at,
                            ),
                        )
                    )
                if checked_decision.state == "expired":
                    return _view(
                        await uow.policies.update(
                            tenant_id,
                            replace(
                                candidate,
                                state=CatalogProposalPolicyState.EXPIRED,
                                terminal_at=applied_at,
                            ),
                        )
                    )

                current = _active_fact(
                    await uow.policies.get_active(tenant_id, for_update=True),
                    tenant_id,
                )
                if not _base_matches(candidate, current):
                    stale = await uow.policies.update(
                        tenant_id,
                        replace(
                            candidate,
                            state=CatalogProposalPolicyState.STALE,
                            terminal_at=applied_at,
                        ),
                    )
                    return _view(stale)
                if current is not None:
                    await uow.policies.update(
                        tenant_id,
                        replace(
                            current,
                            state=CatalogProposalPolicyState.SUPERSEDED,
                            terminal_at=applied_at,
                        ),
                    )
                active = await uow.policies.update(
                    tenant_id,
                    replace(
                        candidate,
                        state=CatalogProposalPolicyState.ACTIVE,
                        activated_at=applied_at,
                    ),
                )
                await uow.bus.publish(
                    CatalogProposalPolicyActivated(
                        tenant_id=tenant_id,
                        occurred_at=applied_at,
                        policy_version_id=active.policy_version_id,
                        content_hash=active.content_hash,
                    )
                )
                return _view(active)
        except Exception as error:  # noqa: BLE001 -- 仓储错误统一脱敏
            _raise_storage_error(error)


__all__ = ("CatalogProposalServiceImpl",)
