"""Catalog Product Proposal 的 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import Any, TypeVar, cast

from sqlalchemy import Select, and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.products.models import (
    CatalogCultivationCase,
    CatalogProductProposal,
    CatalogProductProposalState,
    CatalogProposalEvaluation,
    CatalogProposalPolicyState,
    CatalogProposalPolicyVersion,
)
from domains.products.repository import (
    CatalogPage,
    CatalogPageCursor,
    CatalogPageStream,
)
from domains.products.schemas import (
    CatalogBlockedFactsInput,
    CatalogClusterFactsInput,
    CatalogProposalPolicyContent,
    CatalogProposalRuleResult,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    ApprovalPackageRow,
    CatalogCultivationCaseRow,
    CatalogProductProposalRow,
    CatalogProposalEvaluationRow,
    CatalogProposalPolicyVersionRow,
    WorkflowRunRow,
)
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    RunId,
    TenantId,
)

_logger = logging.getLogger("infra.db.repositories.catalog_products")
RowT = TypeVar("RowT")
EntityT = TypeVar("EntityT")


def _constraint_name(error: BaseException) -> str | None:
    pending: list[object] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if nested is not None:
                pending.append(nested)
    return None


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 200:
        raise ValueError("limit 必须是 1..200 的整数")
    return value


def _policy_from_row(
    row: CatalogProposalPolicyVersionRow,
) -> CatalogProposalPolicyVersion:
    return CatalogProposalPolicyVersion(
        tenant_id=TenantId(row.tenant_id),
        policy_version_id=CatalogProposalPolicyVersionId(row.policy_version_id),
        content=CatalogProposalPolicyContent.model_validate(row.content),
        content_hash=row.content_hash,
        base_active_version_id=(
            CatalogProposalPolicyVersionId(row.base_active_version_id)
            if row.base_active_version_id is not None
            else None
        ),
        proposed_by=EmployeeId(row.proposed_by),
        creation_key=row.creation_key,
        creation_request_hash=row.creation_request_hash,
        approval_id=ApprovalId(row.approval_id)
        if row.approval_id is not None
        else None,
        state=CatalogProposalPolicyState(row.state),
        created_at=row.created_at,
        activated_at=row.activated_at,
        terminal_at=row.terminal_at,
    )


def _evaluation_from_row(
    row: CatalogProposalEvaluationRow,
) -> CatalogProposalEvaluation:
    facts_json = json.dumps(row.facts, ensure_ascii=False, separators=(",", ":"))
    facts = (
        CatalogClusterFactsInput.model_validate_json(facts_json)
        if row.blocked_reason is None
        else CatalogBlockedFactsInput.model_validate_json(facts_json)
    )
    rules = tuple(
        CatalogProposalRuleResult.model_validate(item) for item in row.rule_results
    )
    return CatalogProposalEvaluation(
        tenant_id=TenantId(row.tenant_id),
        evaluation_id=CatalogProposalEvaluationId(row.evaluation_id),
        cluster_id=NeedClusterId(row.cluster_id),
        policy_version_id=CatalogProposalPolicyVersionId(row.policy_version_id),
        facts_hash=row.facts_hash,
        facts=facts,
        rule_results=rules,
        overall_passed=row.overall_passed,
        blocked_reason=cast(Any, row.blocked_reason),
        proposed_by_run=RunId(row.proposed_by_run),
        created_at=row.created_at,
    )


def _proposal_from_row(row: CatalogProductProposalRow) -> CatalogProductProposal:
    return CatalogProductProposal(
        tenant_id=TenantId(row.tenant_id),
        proposal_id=CatalogProductProposalId(row.proposal_id),
        evaluation_id=CatalogProposalEvaluationId(row.evaluation_id),
        cluster_id=NeedClusterId(row.cluster_id),
        policy_version_id=CatalogProposalPolicyVersionId(row.policy_version_id),
        facts_hash=row.facts_hash,
        owner_employee=EmployeeId(row.owner_employee),
        proposed_by_run=RunId(row.proposed_by_run),
        approval_id=ApprovalId(row.approval_id)
        if row.approval_id is not None
        else None,
        approval_request_hash=row.approval_request_hash,
        state=CatalogProductProposalState(row.state),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _case_from_row(row: CatalogCultivationCaseRow) -> CatalogCultivationCase:
    if not isinstance(row.evidence_refs, list) or any(
        not isinstance(item, str) for item in row.evidence_refs
    ):
        raise ValidationError("培养 Case evidence_refs 持久化形状无效")
    return CatalogCultivationCase(
        tenant_id=TenantId(row.tenant_id),
        cultivation_case_id=CatalogCultivationCaseId(row.cultivation_case_id),
        proposal_id=CatalogProductProposalId(row.proposal_id),
        approval_id=ApprovalId(row.approval_id),
        cluster_id=NeedClusterId(row.cluster_id),
        policy_version_id=CatalogProposalPolicyVersionId(row.policy_version_id),
        facts_hash=row.facts_hash,
        evidence_refs=tuple(row.evidence_refs),
        state=row.state,
        queued_at=row.queued_at,
    )


class _CatalogRepository(TenantScopedRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__(tenant_id)
        self._session = session
        self._factory = session_factory

    def _read_allowed(self, tenant_id: TenantId) -> bool:
        return tenant_id == self._tenant_id

    def _require_write_tenant(
        self, tenant_id: TenantId, entity_tenant: TenantId
    ) -> None:
        if tenant_id == self._tenant_id == entity_tenant:
            return
        _logger.critical(
            "检测到 Catalog 跨租户写入",
            extra={"tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")

    def _cursor(
        self, cursor: CatalogPageCursor | None, stream: CatalogPageStream
    ) -> CatalogPageCursor | None:
        if cursor is None:
            return None
        if cursor.tenant_id != self._tenant_id:
            raise TenantIsolationViolation("Catalog 游标不可跨租户使用")
        if cursor.stream != stream:
            raise ValueError("Catalog 游标与查询流不匹配")
        return cursor

    async def _fresh_scalar(self, statement: Select[tuple[RowT]]) -> RowT | None:
        async with self._factory() as session, session.begin():
            return (await session.execute(statement)).scalar_one_or_none()

    async def _insert_with_recovery(
        self,
        row: object,
        *,
        expected_constraints: frozenset[str],
        recover: Callable[[], Awaitable[EntityT | None]],
        matches: Callable[[EntityT], bool],
        conflict_message: str,
    ) -> EntityT | None:
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as error:
            if _constraint_name(error) not in expected_constraints:
                raise
            canonical = await recover()
            if canonical is None or not matches(canonical):
                raise IdempotencyConflict(conflict_message) from None
            return canonical
        return None


class CatalogPolicyRepositoryImpl(_CatalogRepository):
    async def lock_policy_namespace(self, tenant_id: TenantId) -> None:
        self._require_write_tenant(tenant_id, tenant_id)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
            {"identity": f"tradeos:catalog-policy-v1:{tenant_id}"},
        )

    async def add(
        self, tenant_id: TenantId, policy: CatalogProposalPolicyVersion
    ) -> CatalogProposalPolicyVersion:
        self._require_write_tenant(tenant_id, policy.tenant_id)
        policy.__post_init__()
        row = CatalogProposalPolicyVersionRow(
            tenant_id=str(tenant_id),
            policy_version_id=str(policy.policy_version_id),
            content=policy.content.model_dump(mode="json"),
            content_hash=policy.content_hash,
            base_active_version_id=policy.base_active_version_id,
            proposed_by=policy.proposed_by,
            creation_key=policy.creation_key,
            creation_request_hash=policy.creation_request_hash,
            approval_id=policy.approval_id,
            state=policy.state.value,
            created_at=policy.created_at,
            activated_at=policy.activated_at,
            terminal_at=policy.terminal_at,
        )

        async def recover() -> CatalogProposalPolicyVersion | None:
            found = await self._fresh_scalar(
                select(CatalogProposalPolicyVersionRow).where(
                    CatalogProposalPolicyVersionRow.tenant_id == str(self._tenant_id),
                    CatalogProposalPolicyVersionRow.proposed_by
                    == str(policy.proposed_by),
                    CatalogProposalPolicyVersionRow.creation_key == policy.creation_key,
                )
            )
            return None if found is None else _policy_from_row(found)

        canonical = await self._insert_with_recovery(
            row,
            expected_constraints=frozenset(
                {
                    "pk_catalog_proposal_policy_versions",
                    "uq_catalog_policy_creation_key",
                }
            ),
            recover=recover,
            matches=lambda item: (
                item.content_hash == policy.content_hash
                and item.content == policy.content
                and item.base_active_version_id == policy.base_active_version_id
                and item.creation_request_hash == policy.creation_request_hash
            ),
            conflict_message="策略创建键已绑定不同不可变内容",
        )
        return (
            policy
            if canonical is None
            else cast(CatalogProposalPolicyVersion, canonical)
        )

    async def _get(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        *,
        lock: bool,
    ) -> CatalogProposalPolicyVersion | None:
        if not self._read_allowed(tenant_id):
            return None
        statement = self.scoped_query(CatalogProposalPolicyVersionRow).where(
            CatalogProposalPolicyVersionRow.policy_version_id == str(policy_version_id)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _policy_from_row(row)

    async def get(
        self, tenant_id: TenantId, policy_version_id: CatalogProposalPolicyVersionId
    ) -> CatalogProposalPolicyVersion | None:
        return await self._get(tenant_id, policy_version_id, lock=False)

    async def get_for_update(
        self, tenant_id: TenantId, policy_version_id: CatalogProposalPolicyVersionId
    ) -> CatalogProposalPolicyVersion | None:
        return await self._get(tenant_id, policy_version_id, lock=True)

    async def get_active(
        self, tenant_id: TenantId, *, for_update: bool = False
    ) -> CatalogProposalPolicyVersion | None:
        if not self._read_allowed(tenant_id):
            return None
        statement = self.scoped_query(CatalogProposalPolicyVersionRow).where(
            CatalogProposalPolicyVersionRow.state
            == CatalogProposalPolicyState.ACTIVE.value
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _policy_from_row(row)

    async def get_by_creation_key(
        self, tenant_id: TenantId, proposed_by: EmployeeId, creation_key: str
    ) -> CatalogProposalPolicyVersion | None:
        if not self._read_allowed(tenant_id):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(CatalogProposalPolicyVersionRow).where(
                    CatalogProposalPolicyVersionRow.proposed_by == str(proposed_by),
                    CatalogProposalPolicyVersionRow.creation_key == creation_key,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _policy_from_row(row)

    async def update(
        self, tenant_id: TenantId, policy: CatalogProposalPolicyVersion
    ) -> CatalogProposalPolicyVersion:
        self._require_write_tenant(tenant_id, policy.tenant_id)
        policy.__post_init__()
        current = await self.get_for_update(tenant_id, policy.policy_version_id)
        if current is None:
            raise ValidationError("Catalog 策略不存在")
        immutable_current = (
            current.content,
            current.content_hash,
            current.base_active_version_id,
            current.proposed_by,
            current.creation_key,
            current.creation_request_hash,
            current.created_at,
        )
        immutable_desired = (
            policy.content,
            policy.content_hash,
            policy.base_active_version_id,
            policy.proposed_by,
            policy.creation_key,
            policy.creation_request_hash,
            policy.created_at,
        )
        if immutable_current != immutable_desired:
            raise ValidationError("Catalog 策略不可变字段不得修改")
        if current == policy:
            return current
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(
                    update(CatalogProposalPolicyVersionRow)
                    .where(
                        CatalogProposalPolicyVersionRow.tenant_id
                        == str(self._tenant_id),
                        CatalogProposalPolicyVersionRow.policy_version_id
                        == str(policy.policy_version_id),
                    )
                    .values(
                        approval_id=policy.approval_id,
                        state=policy.state.value,
                        activated_at=policy.activated_at,
                        terminal_at=policy.terminal_at,
                    )
                    .returning(CatalogProposalPolicyVersionRow)
                )
                row = result.scalar_one()
        except IntegrityError as error:
            if _constraint_name(error) != "uq_catalog_policy_approval":
                raise
            found = await self._fresh_scalar(
                select(CatalogProposalPolicyVersionRow).where(
                    CatalogProposalPolicyVersionRow.tenant_id == str(self._tenant_id),
                    CatalogProposalPolicyVersionRow.approval_id
                    == str(policy.approval_id),
                )
            )
            if found is None:
                raise IdempotencyConflict("策略审批绑定冲突") from None
            canonical = _policy_from_row(found)
            if (
                canonical.policy_version_id != policy.policy_version_id
                or canonical.content_hash != policy.content_hash
                or canonical.creation_request_hash != policy.creation_request_hash
            ):
                raise IdempotencyConflict("策略审批已绑定不同不可变 subject") from None
            return canonical
        return _policy_from_row(row)

    async def bind_approval(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
        approval_id: ApprovalId,
        expected_request_hash: str,
    ) -> CatalogProposalPolicyVersion | None:
        self._require_write_tenant(tenant_id, tenant_id)
        policy = await self.get_for_update(tenant_id, policy_version_id)
        if policy is None:
            return None
        if policy.creation_request_hash != expected_request_hash:
            raise IdempotencyConflict("策略审批请求摘要不匹配")
        approval = (
            await self._session.execute(
                select(ApprovalPackageRow)
                .where(
                    ApprovalPackageRow.tenant_id == str(self._tenant_id),
                    ApprovalPackageRow.approval_id == str(approval_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        change = None if approval is None else approval.proposed_change
        if (
            approval is None
            or not isinstance(change, dict)
            or approval.contract_namespace != "catalog-policy-v1"
            or approval.approval_type != "catalog_proposal_policy_change"
            or approval.request_hash != expected_request_hash
            or change.get("schema_version") != "catalog-policy-v1"
            or change.get("tenant_id") != str(tenant_id)
            or change.get("approval_type") != approval.approval_type
            or change.get("policy_version_id") != str(policy.policy_version_id)
            or change.get("content_hash") != policy.content_hash
            or change.get("request_hash") != expected_request_hash
            or approval.change_set_ref
            != f"catalog-policy:{policy.policy_version_id}:{policy.content_hash}"
            or approval.proposed_by_run is not None
            or approval.proposed_by_employee != str(policy.proposed_by)
            or approval.owner_employee != str(policy.proposed_by)
        ):
            raise IdempotencyConflict("策略审批不可变 subject 不匹配")
        if policy.approval_id is not None:
            if policy.approval_id != approval_id:
                raise IdempotencyConflict("策略审批已绑定不同不可变 subject")
            return policy
        if policy.state is not CatalogProposalPolicyState.PENDING_APPROVAL:
            raise InvalidStateTransition("策略状态不允许绑定审批")
        if approval.state not in {"pending", "approved", "rejected", "expired"}:
            raise InvalidStateTransition("审批状态不允许首次绑定策略")
        return await self.update(
            tenant_id,
            replace(policy, approval_id=approval_id),
        )

    async def _page(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None,
        ascending: bool,
    ) -> CatalogPage[CatalogProposalPolicyVersion]:
        checked = _limit(limit)
        stream: CatalogPageStream = "pending_policies" if ascending else "policies"
        cursor = self._cursor(cursor, stream)
        if not self._read_allowed(tenant_id):
            return CatalogPage((), None)
        statement = self.scoped_query(CatalogProposalPolicyVersionRow)
        if ascending:
            statement = statement.where(
                CatalogProposalPolicyVersionRow.state == "pending_approval"
            )
        if cursor is not None:
            comparison = (
                or_(
                    CatalogProposalPolicyVersionRow.created_at > cursor.position_at,
                    and_(
                        CatalogProposalPolicyVersionRow.created_at
                        == cursor.position_at,
                        CatalogProposalPolicyVersionRow.policy_version_id
                        > cursor.entity_id,
                    ),
                )
                if ascending
                else or_(
                    CatalogProposalPolicyVersionRow.created_at < cursor.position_at,
                    and_(
                        CatalogProposalPolicyVersionRow.created_at
                        == cursor.position_at,
                        CatalogProposalPolicyVersionRow.policy_version_id
                        < cursor.entity_id,
                    ),
                )
            )
            statement = statement.where(comparison)
        order = (
            (
                CatalogProposalPolicyVersionRow.created_at.asc(),
                CatalogProposalPolicyVersionRow.policy_version_id.asc(),
            )
            if ascending
            else (
                CatalogProposalPolicyVersionRow.created_at.desc(),
                CatalogProposalPolicyVersionRow.policy_version_id.desc(),
            )
        )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(*order).limit(checked + 1)
                )
            ).scalars()
        )
        items = tuple(_policy_from_row(row) for row in rows[:checked])
        next_cursor = None
        if len(rows) > checked:
            last = items[-1]
            next_cursor = CatalogPageCursor(
                tenant_id, stream, last.created_at, str(last.policy_version_id)
            )
        return CatalogPage(items, next_cursor)

    async def list_versions(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProposalPolicyVersion]:
        return await self._page(tenant_id, limit=limit, cursor=cursor, ascending=False)

    async def list_pending_reconciliation(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProposalPolicyVersion]:
        return await self._page(tenant_id, limit=limit, cursor=cursor, ascending=True)


class CatalogEvaluationRepositoryImpl(_CatalogRepository):
    async def require_trusted_catalog_evaluation_run(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        cluster_id: NeedClusterId,
    ) -> None:
        if not self._read_allowed(tenant_id):
            raise ValidationError("目录评估 Run 不可信")
        trusted = (
            await self._session.execute(
                self.scoped_query(WorkflowRunRow).where(
                    WorkflowRunRow.run_id == str(run_id),
                    WorkflowRunRow.workflow_type == "catalog_cluster_evaluation",
                    WorkflowRunRow.workflow_version == 1,
                    WorkflowRunRow.subject_ref == str(cluster_id),
                    WorkflowRunRow.status == "running",
                    WorkflowRunRow.current_step == "evaluate",
                )
            )
        ).scalar_one_or_none()
        if trusted is None:
            raise ValidationError("目录评估 Run 不可信")

    async def add(
        self, tenant_id: TenantId, evaluation: CatalogProposalEvaluation
    ) -> CatalogProposalEvaluation:
        self._require_write_tenant(tenant_id, evaluation.tenant_id)
        evaluation.__post_init__()
        row = CatalogProposalEvaluationRow(
            tenant_id=str(tenant_id),
            evaluation_id=str(evaluation.evaluation_id),
            cluster_id=str(evaluation.cluster_id),
            policy_version_id=str(evaluation.policy_version_id),
            facts_hash=evaluation.facts_hash,
            facts=evaluation.facts.model_dump(mode="json"),
            rule_results=[
                item.model_dump(mode="json") for item in evaluation.rule_results
            ],
            overall_passed=evaluation.overall_passed,
            blocked_reason=evaluation.blocked_reason,
            proposed_by_run=str(evaluation.proposed_by_run),
            created_at=evaluation.created_at,
        )

        async def recover() -> CatalogProposalEvaluation | None:
            found = await self._fresh_scalar(
                select(CatalogProposalEvaluationRow).where(
                    CatalogProposalEvaluationRow.tenant_id == str(self._tenant_id),
                    CatalogProposalEvaluationRow.cluster_id
                    == str(evaluation.cluster_id),
                    CatalogProposalEvaluationRow.policy_version_id
                    == str(evaluation.policy_version_id),
                    CatalogProposalEvaluationRow.facts_hash == evaluation.facts_hash,
                )
            )
            return None if found is None else _evaluation_from_row(found)

        canonical = await self._insert_with_recovery(
            row,
            expected_constraints=frozenset(
                {"pk_catalog_proposal_evaluations", "uq_catalog_evaluation_facts"}
            ),
            recover=recover,
            matches=lambda item: (
                item.cluster_id == evaluation.cluster_id
                and item.policy_version_id == evaluation.policy_version_id
                and item.facts_hash == evaluation.facts_hash
                and item.facts == evaluation.facts
                and item.rule_results == evaluation.rule_results
                and item.overall_passed == evaluation.overall_passed
                and item.blocked_reason == evaluation.blocked_reason
                and item.proposed_by_run == evaluation.proposed_by_run
            ),
            conflict_message="评估唯一键已绑定不同不可变快照",
        )
        return (
            evaluation
            if canonical is None
            else cast(CatalogProposalEvaluation, canonical)
        )

    async def get(
        self, tenant_id: TenantId, evaluation_id: CatalogProposalEvaluationId
    ) -> CatalogProposalEvaluation | None:
        if not self._read_allowed(tenant_id):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(CatalogProposalEvaluationRow).where(
                    CatalogProposalEvaluationRow.evaluation_id == str(evaluation_id)
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _evaluation_from_row(row)

    async def get_by_subject(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
        policy_version_id: CatalogProposalPolicyVersionId,
        facts_hash: str,
    ) -> CatalogProposalEvaluation | None:
        if not self._read_allowed(tenant_id):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(CatalogProposalEvaluationRow).where(
                    CatalogProposalEvaluationRow.cluster_id == str(cluster_id),
                    CatalogProposalEvaluationRow.policy_version_id
                    == str(policy_version_id),
                    CatalogProposalEvaluationRow.facts_hash == facts_hash,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _evaluation_from_row(row)

    async def list_evaluations(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProposalEvaluation]:
        checked = _limit(limit)
        cursor = self._cursor(cursor, "evaluations")
        if not self._read_allowed(tenant_id):
            return CatalogPage((), None)
        statement = self.scoped_query(CatalogProposalEvaluationRow)
        if cursor is not None:
            statement = statement.where(
                or_(
                    CatalogProposalEvaluationRow.created_at < cursor.position_at,
                    and_(
                        CatalogProposalEvaluationRow.created_at == cursor.position_at,
                        CatalogProposalEvaluationRow.evaluation_id < cursor.entity_id,
                    ),
                )
            )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(
                        CatalogProposalEvaluationRow.created_at.desc(),
                        CatalogProposalEvaluationRow.evaluation_id.desc(),
                    ).limit(checked + 1)
                )
            ).scalars()
        )
        items = tuple(_evaluation_from_row(row) for row in rows[:checked])
        next_cursor = (
            CatalogPageCursor(
                tenant_id,
                "evaluations",
                items[-1].created_at,
                str(items[-1].evaluation_id),
            )
            if len(rows) > checked
            else None
        )
        return CatalogPage(items, next_cursor)


class CatalogProductProposalRepositoryImpl(_CatalogRepository):
    async def add(
        self, tenant_id: TenantId, proposal: CatalogProductProposal
    ) -> CatalogProductProposal:
        self._require_write_tenant(tenant_id, proposal.tenant_id)
        proposal.__post_init__()
        row = CatalogProductProposalRow(
            tenant_id=str(tenant_id),
            proposal_id=str(proposal.proposal_id),
            evaluation_id=str(proposal.evaluation_id),
            cluster_id=str(proposal.cluster_id),
            policy_version_id=str(proposal.policy_version_id),
            facts_hash=proposal.facts_hash,
            owner_employee=str(proposal.owner_employee),
            proposed_by_run=str(proposal.proposed_by_run),
            approval_id=proposal.approval_id,
            approval_request_hash=proposal.approval_request_hash,
            state=proposal.state.value,
            created_at=proposal.created_at,
            updated_at=proposal.updated_at,
        )

        async def recover() -> CatalogProductProposal | None:
            found = await self._fresh_scalar(
                select(CatalogProductProposalRow).where(
                    CatalogProductProposalRow.tenant_id == str(self._tenant_id),
                    CatalogProductProposalRow.evaluation_id
                    == str(proposal.evaluation_id),
                )
            )
            return None if found is None else _proposal_from_row(found)

        canonical = await self._insert_with_recovery(
            row,
            expected_constraints=frozenset(
                {
                    "pk_catalog_product_proposals",
                    "uq_catalog_product_proposal_evaluation",
                }
            ),
            recover=recover,
            matches=lambda item: (
                item.evaluation_id == proposal.evaluation_id
                and item.cluster_id == proposal.cluster_id
                and item.policy_version_id == proposal.policy_version_id
                and item.facts_hash == proposal.facts_hash
                and item.owner_employee == proposal.owner_employee
                and item.proposed_by_run == proposal.proposed_by_run
            ),
            conflict_message="提案唯一键已绑定不同不可变 subject",
        )
        return (
            proposal if canonical is None else cast(CatalogProductProposal, canonical)
        )

    async def _get(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId, *, lock: bool
    ) -> CatalogProductProposal | None:
        if not self._read_allowed(tenant_id):
            return None
        statement = self.scoped_query(CatalogProductProposalRow).where(
            CatalogProductProposalRow.proposal_id == str(proposal_id)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else _proposal_from_row(row)

    async def get(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogProductProposal | None:
        return await self._get(tenant_id, proposal_id, lock=False)

    async def get_for_update(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogProductProposal | None:
        return await self._get(tenant_id, proposal_id, lock=True)

    async def get_by_evaluation(
        self, tenant_id: TenantId, evaluation_id: CatalogProposalEvaluationId
    ) -> CatalogProductProposal | None:
        if not self._read_allowed(tenant_id):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(CatalogProductProposalRow).where(
                    CatalogProductProposalRow.evaluation_id == str(evaluation_id)
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _proposal_from_row(row)

    async def update(
        self, tenant_id: TenantId, proposal: CatalogProductProposal
    ) -> CatalogProductProposal:
        self._require_write_tenant(tenant_id, proposal.tenant_id)
        proposal.__post_init__()
        current = await self.get_for_update(tenant_id, proposal.proposal_id)
        if current is None:
            raise ValidationError("Catalog 提案不存在")
        if (
            current.evaluation_id,
            current.cluster_id,
            current.policy_version_id,
            current.facts_hash,
            current.owner_employee,
            current.proposed_by_run,
            current.created_at,
        ) != (
            proposal.evaluation_id,
            proposal.cluster_id,
            proposal.policy_version_id,
            proposal.facts_hash,
            proposal.owner_employee,
            proposal.proposed_by_run,
            proposal.created_at,
        ):
            raise ValidationError("Catalog 提案不可变字段不得修改")
        if current == proposal:
            return current
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(
                    update(CatalogProductProposalRow)
                    .where(
                        CatalogProductProposalRow.tenant_id == str(self._tenant_id),
                        CatalogProductProposalRow.proposal_id
                        == str(proposal.proposal_id),
                    )
                    .values(
                        approval_id=proposal.approval_id,
                        approval_request_hash=proposal.approval_request_hash,
                        state=proposal.state.value,
                        updated_at=proposal.updated_at,
                    )
                    .returning(CatalogProductProposalRow)
                )
                row = result.scalar_one()
        except IntegrityError as error:
            if _constraint_name(error) != "uq_catalog_product_proposal_approval":
                raise
            found = await self._fresh_scalar(
                select(CatalogProductProposalRow).where(
                    CatalogProductProposalRow.tenant_id == str(self._tenant_id),
                    CatalogProductProposalRow.approval_id == str(proposal.approval_id),
                )
            )
            if found is None:
                raise IdempotencyConflict("提案审批绑定冲突") from None
            canonical = _proposal_from_row(found)
            if (
                canonical.proposal_id,
                canonical.policy_version_id,
                canonical.facts_hash,
                canonical.approval_request_hash,
            ) != (
                proposal.proposal_id,
                proposal.policy_version_id,
                proposal.facts_hash,
                proposal.approval_request_hash,
            ):
                raise IdempotencyConflict("提案审批已绑定不同不可变 subject") from None
            return canonical
        return _proposal_from_row(row)

    async def bind_approval(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        approval_id: ApprovalId,
        expected_request_hash: str,
        bound_at: datetime,
    ) -> CatalogProductProposal | None:
        self._require_write_tenant(tenant_id, tenant_id)
        proposal = await self.get_for_update(tenant_id, proposal_id)
        if proposal is None:
            return None
        approval = (
            await self._session.execute(
                select(ApprovalPackageRow)
                .where(
                    ApprovalPackageRow.tenant_id == str(self._tenant_id),
                    ApprovalPackageRow.approval_id == str(approval_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        change = None if approval is None else approval.proposed_change
        expected_change_set = (
            f"catalog-cultivation:{proposal.proposal_id}:"
            f"{proposal.policy_version_id}:{proposal.facts_hash}"
        )
        if (
            approval is None
            or not isinstance(change, dict)
            or approval.contract_namespace != "catalog-cultivation-v1"
            or approval.approval_type != "catalog_product_cultivation"
            or approval.request_hash != expected_request_hash
            or change.get("schema_version") != "catalog-cultivation-v1"
            or change.get("tenant_id") != str(tenant_id)
            or change.get("approval_type") != approval.approval_type
            or change.get("proposal_id") != str(proposal.proposal_id)
            or change.get("policy_version_id") != str(proposal.policy_version_id)
            or change.get("facts_hash") != proposal.facts_hash
            or change.get("request_hash") != expected_request_hash
            or approval.change_set_ref != expected_change_set
            or approval.proposed_by_employee is not None
            or approval.proposed_by_run != str(proposal.proposed_by_run)
            or approval.owner_employee != str(proposal.owner_employee)
        ):
            raise IdempotencyConflict("提案审批不可变 subject 不匹配")
        if proposal.approval_id is not None:
            if (
                proposal.approval_id != approval_id
                or proposal.approval_request_hash != expected_request_hash
            ):
                raise IdempotencyConflict("提案审批已绑定不同不可变 subject")
            return proposal
        if (
            proposal.state
            is not CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION
        ):
            raise InvalidStateTransition("提案状态不允许绑定审批")
        if approval.state not in {"pending", "approved", "rejected", "expired"}:
            raise InvalidStateTransition("审批状态不允许首次绑定提案")
        return await self.update(
            tenant_id,
            replace(
                proposal,
                approval_id=approval_id,
                approval_request_hash=expected_request_hash,
                state=CatalogProductProposalState.PENDING_REVIEW,
                updated_at=bound_at,
            ),
        )

    async def _page(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None,
        ascending: bool,
    ) -> CatalogPage[CatalogProductProposal]:
        checked = _limit(limit)
        stream: CatalogPageStream = "awaiting_proposals" if ascending else "proposals"
        cursor = self._cursor(cursor, stream)
        if not self._read_allowed(tenant_id):
            return CatalogPage((), None)
        time_col = (
            CatalogProductProposalRow.created_at
            if ascending
            else CatalogProductProposalRow.updated_at
        )
        statement = self.scoped_query(CatalogProductProposalRow)
        if ascending:
            statement = statement.where(
                CatalogProductProposalRow.state == "awaiting_approval_submission"
            )
        if cursor is not None:
            comparison = (
                or_(
                    time_col > cursor.position_at,
                    and_(
                        time_col == cursor.position_at,
                        CatalogProductProposalRow.proposal_id > cursor.entity_id,
                    ),
                )
                if ascending
                else or_(
                    time_col < cursor.position_at,
                    and_(
                        time_col == cursor.position_at,
                        CatalogProductProposalRow.proposal_id < cursor.entity_id,
                    ),
                )
            )
            statement = statement.where(comparison)
        order = (
            (time_col.asc(), CatalogProductProposalRow.proposal_id.asc())
            if ascending
            else (time_col.desc(), CatalogProductProposalRow.proposal_id.desc())
        )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(*order).limit(checked + 1)
                )
            ).scalars()
        )
        items = tuple(_proposal_from_row(row) for row in rows[:checked])
        next_cursor = None
        if len(rows) > checked:
            last = items[-1]
            last_at = last.created_at if ascending else last.updated_at
            next_cursor = CatalogPageCursor(
                tenant_id, stream, last_at, str(last.proposal_id)
            )
        return CatalogPage(items, next_cursor)

    async def list_proposals(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProductProposal]:
        return await self._page(tenant_id, limit=limit, cursor=cursor, ascending=False)

    async def list_awaiting_reconciliation(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogProductProposal]:
        return await self._page(tenant_id, limit=limit, cursor=cursor, ascending=True)


class CatalogCultivationCaseRepositoryImpl(_CatalogRepository):
    async def add(
        self, tenant_id: TenantId, cultivation_case: CatalogCultivationCase
    ) -> CatalogCultivationCase:
        self._require_write_tenant(tenant_id, cultivation_case.tenant_id)
        cultivation_case.__post_init__()
        row = CatalogCultivationCaseRow(
            tenant_id=str(tenant_id),
            cultivation_case_id=str(cultivation_case.cultivation_case_id),
            proposal_id=str(cultivation_case.proposal_id),
            approval_id=str(cultivation_case.approval_id),
            cluster_id=str(cultivation_case.cluster_id),
            policy_version_id=str(cultivation_case.policy_version_id),
            facts_hash=cultivation_case.facts_hash,
            evidence_refs=list(cultivation_case.evidence_refs),
            state=cultivation_case.state,
            queued_at=cultivation_case.queued_at,
        )

        async def recover() -> CatalogCultivationCase | None:
            found = await self._fresh_scalar(
                select(CatalogCultivationCaseRow).where(
                    CatalogCultivationCaseRow.tenant_id == str(self._tenant_id),
                    CatalogCultivationCaseRow.proposal_id
                    == str(cultivation_case.proposal_id),
                )
            )
            return None if found is None else _case_from_row(found)

        canonical = await self._insert_with_recovery(
            row,
            expected_constraints=frozenset(
                {
                    "pk_catalog_cultivation_cases",
                    "uq_catalog_cultivation_proposal",
                    "uq_catalog_cultivation_approval",
                }
            ),
            recover=recover,
            matches=lambda item: (
                item.proposal_id == cultivation_case.proposal_id
                and item.approval_id == cultivation_case.approval_id
                and item.cluster_id == cultivation_case.cluster_id
                and item.policy_version_id == cultivation_case.policy_version_id
                and item.facts_hash == cultivation_case.facts_hash
                and item.evidence_refs == cultivation_case.evidence_refs
            ),
            conflict_message="培养 Case 唯一键已绑定不同不可变 subject",
        )
        return (
            cultivation_case
            if canonical is None
            else cast(CatalogCultivationCase, canonical)
        )

    async def get(
        self, tenant_id: TenantId, cultivation_case_id: CatalogCultivationCaseId
    ) -> CatalogCultivationCase | None:
        if not self._read_allowed(tenant_id):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(CatalogCultivationCaseRow).where(
                    CatalogCultivationCaseRow.cultivation_case_id
                    == str(cultivation_case_id)
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _case_from_row(row)

    async def get_by_proposal(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogCultivationCase | None:
        if not self._read_allowed(tenant_id):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(CatalogCultivationCaseRow).where(
                    CatalogCultivationCaseRow.proposal_id == str(proposal_id)
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _case_from_row(row)

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> CatalogCultivationCase | None:
        if not self._read_allowed(tenant_id):
            return None
        row = (
            await self._session.execute(
                self.scoped_query(CatalogCultivationCaseRow).where(
                    CatalogCultivationCaseRow.approval_id == str(approval_id)
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _case_from_row(row)

    async def list_cases(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogPageCursor | None = None,
    ) -> CatalogPage[CatalogCultivationCase]:
        checked = _limit(limit)
        cursor = self._cursor(cursor, "cultivation_cases")
        if not self._read_allowed(tenant_id):
            return CatalogPage((), None)
        statement = self.scoped_query(CatalogCultivationCaseRow)
        if cursor is not None:
            statement = statement.where(
                or_(
                    CatalogCultivationCaseRow.queued_at < cursor.position_at,
                    and_(
                        CatalogCultivationCaseRow.queued_at == cursor.position_at,
                        CatalogCultivationCaseRow.cultivation_case_id
                        < cursor.entity_id,
                    ),
                )
            )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(
                        CatalogCultivationCaseRow.queued_at.desc(),
                        CatalogCultivationCaseRow.cultivation_case_id.desc(),
                    ).limit(checked + 1)
                )
            ).scalars()
        )
        items = tuple(_case_from_row(row) for row in rows[:checked])
        next_cursor = (
            CatalogPageCursor(
                tenant_id,
                "cultivation_cases",
                items[-1].queued_at,
                str(items[-1].cultivation_case_id),
            )
            if len(rows) > checked
            else None
        )
        return CatalogPage(items, next_cursor)


__all__ = (
    "CatalogCultivationCaseRepositoryImpl",
    "CatalogEvaluationRepositoryImpl",
    "CatalogPolicyRepositoryImpl",
    "CatalogProductProposalRepositoryImpl",
)
