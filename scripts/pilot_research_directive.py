"""为本机肯尼亚小额试验准备并确认固定的只研究指令。"""

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

from agent_runtime.assistant.discovery_queries import build_discovery_queries
from apps.scheduler_worker.research_acceptance_dependencies import (
    build_acceptance_readers,
)
from domains.directives.schemas import DemandDiscoveryPlanInput
from infra.db.session import create_engine_from
from infra.db.tables import BossDirectiveRow, DirectiveProposalRow, EmployeeRow
from infra.pilot.config import PilotConfig
from shared.schemas.identifiers import EmployeeId, TenantId

RAW_TEXT = "确认研究提案"
SUMMARY = (
    "首轮仅研究肯尼亚太阳能或电动三轮车的进口商与经销渠道；"
    "最多三次免费公开搜索、读取三页，排除明显自产、二手和燃油车型。"
    "网页事实只作为待核对线索；不验证邮箱、不创建触达序列、不发邮件。"
)
CHANGES = [
    "激活肯尼亚只研究指令；三条基础检索线路各最多一条结果，总计最多三次搜索、三页原件。",
    "最多生成三条待核对需求信号和三条假设；搜索方向不代表所在地或购买意向。",
    "不启用联系人发现、邮箱验证、Campaign 或发信；旧的发送门禁不变。",
]
PARSED_BY = "codex:kenya-research-confirmed-v1"


def plan() -> DemandDiscoveryPlanInput:
    """返回用户已看到的首轮有界范围，不能从外部参数扩大预算。"""
    return DemandDiscoveryPlanInput(
        objective="研究肯尼亚太阳能或电动三轮车潜在进口商及经销商",
        queries=build_discovery_queries(
            countries=("KE",),
            categories=("solar electric three-wheeler",),
            max_queries=3,
            result_limit=1,
        ),
        target_countries=("KE",),
        target_categories=("solar electric three-wheeler",),
        excluded_countries=(),
        excluded_categories=("used vehicle", "fuel three-wheeler"),
        max_search_queries=3,
        max_pages_read=3,
        max_signals=3,
        max_hypotheses=3,
        minimum_confidence_tier="low",
        strategy_group="kenya_solar_three_wheeler_pilot",
        campaign_id="",
        role_hints=(),
        assessment_ref="",
        execution_mode="research_only",
    )


async def execute(profile: Path, action: str, proposal_id: str | None) -> dict[str, object]:
    """仅经指令域写库；未确认提案不得激活，也不访问外部搜索。"""
    config = PilotConfig.read(profile / "config.json")
    tenant = TenantId(config.tenant_id)
    engine = create_engine_from(config.runtime_environment()["DATABASE_URL"])
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        reader, _, _ = build_acceptance_readers(factory, tenant)
        async with factory() as session:
            active = (
                await session.execute(
                    select(BossDirectiveRow.directive_id).where(
                        BossDirectiveRow.tenant_id == tenant
                    )
                )
            ).first()
            pending = (
                await session.execute(
                    select(DirectiveProposalRow.proposal_id).where(
                        DirectiveProposalRow.tenant_id == tenant,
                        DirectiveProposalRow.state == "pending_confirmation",
                    )
                )
            ).scalars().all()
        if action == "prepare":
            if active is not None or pending:
                return {"status": "not_prepared", "reason": "directive_or_pending_proposal_exists"}
            created = await reader.directives.submit_discovery_proposal(
                tenant, RAW_TEXT, plan(), SUMMARY, CHANGES, PARSED_BY
            )
            return {
                "status": "pending_confirmation",
                "proposal_id": created,
                "scope": "research_only",
                "searches_limit": 3,
                "pages_limit": 3,
                "outreach": "not_run",
            }
        if action != "confirm" or proposal_id is None:
            return {"status": "not_confirmed", "reason": "input_invalid"}
        view = await reader.directives.get_proposal(tenant, proposal_id)
        fields = view.parsed_fields
        expected_queries = [
            {
                "query": query.query,
                "country": query.country,
                "category": query.category,
                "limit": query.limit,
                "discovery_lane": query.discovery_lane,
            }
            for query in plan().queries
        ]
        if (
            fields.get("execution_mode") != "research_only"
            or fields.get("target_countries") != "KE"
            or fields.get("target_categories") != "solar electric three-wheeler"
            or fields.get("max_search_queries") != "3"
            or fields.get("max_pages_read") != "3"
            or fields.get("max_signals") != "3"
            or fields.get("max_hypotheses") != "3"
            or json.loads(fields.get("queries", "null")) != expected_queries
        ):
            return {"status": "not_confirmed", "reason": "scope_mismatch"}
        async with factory() as session:
            proposal = (
                await session.execute(
                    select(DirectiveProposalRow).where(
                        DirectiveProposalRow.tenant_id == tenant,
                        DirectiveProposalRow.proposal_id == proposal_id,
                    )
                )
            ).scalar_one_or_none()
            boss_ids = (
                await session.execute(
                    select(EmployeeRow.employee_id).where(
                        EmployeeRow.tenant_id == tenant,
                        EmployeeRow.is_active.is_(True),
                        EmployeeRow.role == "boss",
                        EmployeeRow.user_id.is_not(None),
                    )
                )
            ).scalars().all()
        if (
            proposal is None
            or proposal.state != "pending_confirmation"
            or proposal.raw_text != RAW_TEXT
            or proposal.interpretation_summary != SUMMARY
            or proposal.expected_behavior_changes != CHANGES
            or proposal.parsed_by != PARSED_BY
            or len(boss_ids) != 1
        ):
            return {"status": "not_confirmed", "reason": "proposal_or_actor_mismatch"}
        directive_id = await reader.directives.confirm_proposal(
            tenant, proposal_id, EmployeeId(boss_ids[0])
        )
        return {
            "status": "confirmed",
            "proposal_id": proposal_id,
            "directive_id": str(directive_id),
            "scope": "research_only",
            "external_search": "not_run",
            "outreach": "not_run",
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "confirm"))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--proposal-id")
    args = parser.parse_args()
    try:
        if not args.profile.is_absolute():
            raise ValueError("profile_path_invalid")
        result = asyncio.run(execute(args.profile, args.action, args.proposal_id))
    except Exception:  # noqa: BLE001 不输出私有配置、SQL异常或外部凭证
        result = {"status": "not_confirmed", "reason": "operation_failed"}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] in {"pending_confirmation", "confirmed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
