"""只读检查本机研究提案是否具备一次性来源验收的业务前提。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.scheduler_worker.research_acceptance_dependencies import (
    build_acceptance_readers,
)
from domains.compliance.schemas import CountryPolicyAction
from domains.organization.errors import PlaybookNotConfiguredError
from infra.db.session import create_engine_from
from infra.db.tables import (
    BossDirectiveRow,
    DirectiveProposalRow,
    DirectiveVersionRow,
    EmployeeRow,
)
from infra.pilot.config import PilotConfig
from shared.schemas.identifiers import EmployeeId, TenantId


async def inspect(profile: Path) -> dict[str, object]:
    """仅输出批准状态和业务边界，不读取或打印 Provider 密钥。"""
    config = PilotConfig.read(profile / "config.json")
    engine = create_engine_from(config.runtime_environment()["DATABASE_URL"])
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            row = (
                await session.execute(
                    select(DirectiveVersionRow, DirectiveProposalRow, EmployeeRow)
                    .join(
                        BossDirectiveRow,
                        (BossDirectiveRow.tenant_id == DirectiveVersionRow.tenant_id)
                        & (BossDirectiveRow.directive_id == DirectiveVersionRow.directive_id)
                        & (BossDirectiveRow.version == DirectiveVersionRow.version),
                    )
                    .join(
                        DirectiveProposalRow,
                        (DirectiveProposalRow.tenant_id == DirectiveVersionRow.tenant_id)
                        & (DirectiveProposalRow.proposal_id == DirectiveVersionRow.source_proposal_id),
                    )
                    .outerjoin(
                        EmployeeRow,
                        (EmployeeRow.tenant_id == DirectiveVersionRow.tenant_id)
                        & (EmployeeRow.employee_id == DirectiveVersionRow.activated_by),
                    )
                    .where(DirectiveVersionRow.tenant_id == config.tenant_id)
                )
            ).one_or_none()
            if row is None:
                pending = (
                    await session.execute(
                        select(DirectiveProposalRow.proposal_id)
                        .where(
                            DirectiveProposalRow.tenant_id == config.tenant_id,
                            DirectiveProposalRow.state == "pending_confirmation",
                        )
                        .order_by(DirectiveProposalRow.created_at.desc())
                        .limit(5)
                    )
                ).scalars().all()
                return {
                    "status": "not_ready",
                    "reason": "no_active_directive",
                    "pending_proposal_ids": pending,
                }
            version, proposal, actor = row
            content = proposal.parsed_content
            discovery = content.get("demand_discovery") if isinstance(content, dict) else None
            if (
                proposal.state != "confirmed"
                or not isinstance(discovery, dict)
                or discovery.get("execution_mode") != "research_only"
                or actor is None
                or not actor.is_active
                or actor.role != "boss"
                or actor.user_id is None
            ):
                return {"status": "not_ready", "reason": "active_research_approval_missing"}
            tenant = TenantId(config.tenant_id)
            reader, playbook, policy = build_acceptance_readers(factory, tenant)
            employee = EmployeeId(actor.employee_id)
            user = await reader.user_for_employee(tenant, employee)
            plan = await reader.load_confirmed(
                tenant, version.source_proposal_id, user
            )
            playbook.bind_actor(employee)
            reasons: list[str] = []
            for query in plan.queries:
                decision = await policy.decision(
                    tenant, query.country, CountryPolicyAction.PUBLIC_RESEARCH
                )
                if (
                    (not decision.configured or not decision.allowed)
                    and "country_policy_not_allowed" not in reasons
                ):
                    reasons.append("country_policy_not_allowed")
                try:
                    playbook_allowed = await playbook.allows_research(
                        tenant, query.category, query.country
                    )
                except PlaybookNotConfiguredError:
                    if "playbook_not_configured" not in reasons:
                        reasons.append("playbook_not_configured")
                else:
                    if not playbook_allowed and "playbook_not_allowed" not in reasons:
                        reasons.append("playbook_not_allowed")
            if reasons:
                return {"status": "not_ready", "reasons": reasons}
            return {
                "status": "approved_research_proposal_found",
                "proposal_id": version.source_proposal_id,
                "actor_id": actor.employee_id,
                "scope": "research_only",
                "external_search": "not_run",
                "outreach": "not_run",
            }
    finally:
        await engine.dispose()


def main() -> int:
    """诊断失败只返回固定原因，避免泄露私有配置或数据库异常。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.profile.is_absolute():
            raise ValueError("profile_path_invalid")
        result = asyncio.run(inspect(args.profile))
    except Exception as error:  # noqa: BLE001 本地诊断边界不回显配置和异常
        result = {
            "status": "not_ready",
            "reason": "readiness_check_failed",
            "error_type": type(error).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] != "not_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
