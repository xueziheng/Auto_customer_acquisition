"""一次性真实来源验收组合；不注册到普通 scheduler，不持有模型或触达端口。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.scheduler_worker.web_discovery import (
    WebDiscoveryToolComposition,
    WebDiscoveryTools,
    build_web_discovery_tools,
)
from infra.db.tables import SearchQuotaReservationRow, WorkflowRunRow
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id
from tool_gateway.checks.contact_provider import CountryPolicyDecisionReader
from tool_gateway.errors import ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.free_search_contracts import FreeSearchError
from workflows.demand_discovery.ports import DemandDiscoveryTaskReader
from workflows.demand_discovery.steps import PlanSearchStep, _confirmed_plan
from workflows.engine.runner import StepDefinition, WorkflowDefinition, WorkflowRun

WORKFLOW_TYPE = "research_source_acceptance"


def acceptance_definition() -> WorkflowDefinition:
    """复用已确认计划的预算装载；来源验收终态不能被解释为业务研究完成。"""
    return WorkflowDefinition(
        WORKFLOW_TYPE,
        1,
        (
            StepDefinition("plan_search", "acceptance.plan", max_retries=0),
            StepDefinition("execute_search", "acceptance.pages", max_retries=0),
        ),
        {"plan_search": ("execute_search",), "execute_search": ()},
    )


class SourceAcceptanceStep:
    def __init__(
        self, reader: DemandDiscoveryTaskReader, tools: WebDiscoveryTools
    ) -> None:
        self._reader, self._tools = reader, tools

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        """仅保存安全页面元信息；原文只进入不可变 Artifact Store。"""
        plan, _ = await _confirmed_plan(run, self._reader)
        if plan.execution_mode != "research_only":
            raise ValidationError("来源验收只接受研究提案")
        pages: list[dict[str, object]] = []
        searches_used = pages_used = 0
        stop_reason = "pages_only"
        try:
            for query in plan.queries:
                if (
                    searches_used >= plan.max_search_queries
                    or pages_used >= plan.max_pages_read
                ):
                    stop_reason = "budget_exhausted"
                    break
                searches_used += 1
                batch = await self._tools.searcher.search(
                    run.tenant_id,
                    run.run_id,
                    query.query,
                    query.country,
                    query.category,
                    query.limit,
                )
                try:
                    for index in range(len(batch.results)):
                        if pages_used >= plan.max_pages_read:
                            stop_reason = "budget_exhausted"
                            break
                        pages_used += 1
                        snapshot = await self._tools.page_reader.read_page(
                            run.tenant_id, run.run_id, batch, index
                        )
                        pages.append(
                            {
                                "discovery_lane": query.discovery_lane,
                                "source_url": snapshot.url,
                                "observed_at": snapshot.observed_at.isoformat(),
                                "page_hash": snapshot.content_hash,
                                "snapshot_artifact_ref": str(
                                    snapshot.snapshot_artifact_ref
                                ),
                            }
                        )
                finally:
                    self._tools.searcher.release(batch)
        except ToolGatewayError as error:
            stop_reason = (
                error.reason.value
                if isinstance(error, FreeSearchError)
                else error.category.value
            )
        finally:
            self._tools.searcher.discard_all()
        if stop_reason == "pages_only" and not pages:
            stop_reason = "no_results"
        elif stop_reason == "pages_only" and {
            page["discovery_lane"] for page in pages
        } != {"importer", "distributor", "ecommerce"}:
            stop_reason = "partial_sources"
        return (
            "complete",
            None,
            {
                "scope": "pages_only",
                "model": "not_run",
                "outreach": "not_run",
                "completion_reason": stop_reason,
                "searches_used": searches_used,
                "pages_used": pages_used,
                "pages": pages,
            },
        )


class SourceAcceptancePlanStep:
    def __init__(self, reader: DemandDiscoveryTaskReader) -> None:
        self._plan = PlanSearchStep(reader)

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        """借用业务计划校验和预算，不冒充业务research投影或成果。"""
        action, step, patch = await self._plan.execute(run)
        patch.pop("execution_mode", None)
        return (
            action,
            step,
            {**patch, "scope": "pages_only", "model": "not_run", "outreach": "not_run"},
        )


async def run_source_acceptance(
    *,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    actor_id: UserId,
    proposal_id: str,
    reader: DemandDiscoveryTaskReader,
    composition: WebDiscoveryToolComposition,
    country_policy: CountryPolicyDecisionReader,
    fingerprints: HmacFingerprintProvider,
    lease_duration: timedelta,
    now: Callable[[], datetime],
) -> dict[str, object]:
    """仅驱动专用类型；同提案固定幂等键，重启不重新创建可花费预算的 Run。"""
    lock_key = int.from_bytes(
        sha256(f"tradeos:research-source-acceptance:v1:{tenant_id}".encode()).digest()[
            :8
        ],
        "big",
        signed=True,
    )
    async with factory() as lock_session, lock_session.begin():
        acquired = (
            await lock_session.execute(select(func.pg_try_advisory_xact_lock(lock_key)))
        ).scalar_one()
        if not acquired:
            return {
                "status": "not_run",
                "reason": "acceptance_in_progress",
                "model": "not_run",
                "outreach": "not_run",
            }
        plan = await reader.load_confirmed(tenant_id, proposal_id, actor_id)
        if plan.execution_mode != "research_only" or composition.provider != "tavily":
            raise ValidationError("来源验收只接受免费研究组合")
        tools = build_web_discovery_tools(
            factory=factory,
            tenant_id=tenant_id,
            tool_user=UserId(new_id("usr")),
            fingerprints=fingerprints,
            composition=composition,
            country_policy=country_policy,
            lease_duration=lease_duration,
            now=now,
            workflow_type=WORKFLOW_TYPE,
        )
        engine = PostgresWorkflowEngine(
            factory,
            {
                "acceptance.plan": SourceAcceptancePlanStep(reader),
                "acceptance.pages": SourceAcceptanceStep(reader, tools),
            },
            now=now,
        )
        engine.register(acceptance_definition())
        # 一个入口不应推进另一份提案的在途验收。已开始但中断的原提案仅允许同键恢复。
        async with factory() as session:
            other = (
                await session.execute(
                    select(WorkflowRunRow.run_id).where(
                        WorkflowRunRow.tenant_id == tenant_id,
                        WorkflowRunRow.workflow_type == WORKFLOW_TYPE,
                        WorkflowRunRow.status == "running",
                        WorkflowRunRow.subject_ref != proposal_id,
                    )
                )
            ).first()
        if other is not None:
            raise ValidationError("其他来源验收尚未结束")
        run_id = await engine.start(
            tenant_id,
            WORKFLOW_TYPE,
            proposal_id,
            {"proposal_id": proposal_id, "acting_user_id": str(actor_id)},
            f"research-source-acceptance:{proposal_id}",
        )
        try:
            await engine.poll_due(tenant_id, 2)
        except BaseException:
            # 尽力留终态；强杀进程时 ledger 仍保守保留，普通scheduler不注册此类型。
            await engine.cancel(tenant_id, run_id, "acceptance_interrupted")
            raise
        async with factory() as session:
            row = (
                await session.execute(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.tenant_id == tenant_id,
                        WorkflowRunRow.run_id == run_id,
                    )
                )
            ).scalar_one()
            totals = {
                status: count
                for status, count in (
                    await session.execute(
                        select(
                            SearchQuotaReservationRow.status,
                            func.count(),
                        )
                        .where(
                            SearchQuotaReservationRow.tenant_id == tenant_id,
                            SearchQuotaReservationRow.run_id == run_id,
                            SearchQuotaReservationRow.provider == "tavily",
                        )
                        .group_by(SearchQuotaReservationRow.status)
                    )
                ).all()
            }
            return {
                "status": row.status,
                "run_id": str(run_id),
                "scope": "pages_only",
                "model": "not_run",
                "outreach": "not_run",
                "reason": row.context.get(
                    "completion_reason", row.last_error or "execution_incomplete"
                ),
                "pages": row.context.get("pages", []),
                "searches_used": row.context.get("searches_used", 0),
                "pages_used": row.context.get("pages_used", 0),
                "consumed_credits": totals.get("consumed", 0),
                "reserved_credits": totals.get("reserved", 0),
                "uncertain_credits": totals.get("uncertain", 0),
            }


async def run_live_acceptance(
    environ: Mapping[str, str], proposal_id: str, actor_id: str
) -> dict[str, object]:
    """从部署环境真实装配；缺依赖返回 not_run，不造批准、账户或模型。"""
    required = (
        "DATABASE_URL",
        "TRADEOS_TENANT_ID",
        "TAVILY_API_KEY_REF",
        "TOOL_CALL_FINGERPRINT_KEY_REF",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION",
        "TRADEOS_TOOL_LEASE_SECONDS",
        "S3_ENDPOINT",
        "S3_BUCKET_ARTIFACTS",
        "S3_ACCESS_KEY_REF",
        "S3_SECRET_KEY_REF",
        "S3_REGION",
        "RAW_ARTIFACT_MAX_BYTES",
        "GENERATED_ARTIFACT_MAX_BYTES",
        "TRADEOS_DEV_MODE",
    )
    not_run: dict[str, object] = {
        "status": "not_run",
        "reason": "configuration_missing",
        "model": "not_run",
        "outreach": "not_run",
    }
    if any(not environ.get(key) for key in required):
        return not_run
    if environ.get("TRADEOS_TAVILY_EXCLUSIVE_ACCOUNT_CONFIRMED") != "true":
        return {**not_run, "reason": "exclusive_account_not_confirmed"}
    from apps.scheduler_worker.research_acceptance_dependencies import (
        build_acceptance_readers,
    )
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.object_store.config import S3ObjectStoreSettings
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from connectors.tavily.transport import TavilySearchApiTransport
    from connectors.web_search.transport import SafePublicPageHttpTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from infra.db.session import create_engine_from
    from infra.secrets import (
        EnvironmentSecretResolver,
        validate_environment_secret_reference,
    )
    from shared.schemas.identifiers import new_id

    settings = S3ObjectStoreSettings.from_environ(environ)
    for ref in (
        environ["TAVILY_API_KEY_REF"],
        environ["TOOL_CALL_FINGERPRINT_KEY_REF"],
        settings.access_key_ref,
        settings.secret_key_ref,
    ):
        validate_environment_secret_reference(ref)
        if ref not in environ:
            return not_run
    lease = int(environ["TRADEOS_TOOL_LEASE_SECONDS"])
    if not 1 <= lease <= 86400:
        raise ValidationError("验收 lease 无效")
    tenant = TenantId(environ["TRADEOS_TENANT_ID"])
    engine = create_engine_from(environ["DATABASE_URL"])
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        reader, playbook, policy = build_acceptance_readers(factory, tenant)
        plan = await reader.load_confirmed(tenant, proposal_id, UserId(actor_id))
        playbook.bind_actor(EmployeeId(actor_id))
        # 在构造对象存储适配器及解析任何凭证前检查真实政策；Gateway每次仍重查。
        from domains.compliance.schemas import CountryPolicyAction

        for query in plan.queries:
            if not await playbook.allows_research(
                tenant, query.category, query.country
            ):
                return {**not_run, "reason": "playbook_not_allowed"}
            decision = await policy.decision(
                tenant, query.country, CountryPolicyAction.PUBLIC_RESEARCH
            )
            if not decision.configured or not decision.allowed:
                return {**not_run, "reason": "country_policy_not_allowed"}
        secrets = EnvironmentSecretResolver(environ)
        artifacts = RawArtifactStoreImpl(
            lambda bound: SqlAlchemyArtifactUnitOfWork(factory, bound),  # type: ignore[arg-type, return-value]
            S3ObjectBlobTransport(settings, secrets),
            settings.raw_max_bytes,
            lambda: datetime.now(UTC),
            new_id,
        )
        composition = WebDiscoveryToolComposition(
            playbook,
            secrets,
            environ["TAVILY_API_KEY_REF"],
            TavilySearchApiTransport(),
            SafePublicPageHttpTransport(),
            artifacts,
            provider="tavily",
            exclusive_account_confirmed=True,
        )
        fingerprints = HmacFingerprintProvider(
            environ["TOOL_CALL_FINGERPRINT_KEY_VERSION"],
            secrets.resolve(environ["TOOL_CALL_FINGERPRINT_KEY_REF"]).encode(),
        )
        return await run_source_acceptance(
            factory=factory,
            tenant_id=tenant,
            actor_id=UserId(actor_id),
            proposal_id=proposal_id,
            reader=reader,
            composition=composition,
            country_policy=policy,
            fingerprints=fingerprints,
            lease_duration=timedelta(seconds=lease),
            now=lambda: datetime.now(UTC),
        )
    finally:
        await engine.dispose()
