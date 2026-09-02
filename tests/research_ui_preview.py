"""受控 UI 预览：真实 API 路由 + 合成内存依赖，无网络/模型/数据库/发送。

只用于 Task4 人工界面验收，不替代 Task5 真实数据库与实际 Gateway 端到端。
启动：python -m uvicorn tests.research_ui_preview:app --host 127.0.0.1 --port 8184
"""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.trade_manager.agent import TradeManagerAgent
from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.api.research import ResearchAccessService
from domains.demand.schemas import DemandSignalView, ResearchEvidence
from domains.directives.schemas import ProposalView
from domains.employees.permissions import Phase1EmployeeAuthorizer
from domains.prospecting.schemas import ProspectAccountDetailView, ProspectAccountView
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import ProspectAccountId, RunId, new_id
from tests.unit.test_runs_router import TENANT, _identity
from tests.unit.workflows.test_research_discovery import research_plan
from workflows.engine.audit import RunDetailView, RunResearchView, RunSummaryView
from workflows.sourcing_case.application import (
    SourcingAdmissionListView,
    SourcingAdmissionPolicyView,
)

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)
LANES = ("importer", "distributor", "ecommerce")


class Preview:
    """全部业务结果为合成夹具；仅在真实确认路由启动之后向页面公开。"""

    def __init__(self):
        self.proposals = {}
        self.runs = {}
        self.signals = []
        self.accounts = []

    async def parse_discovery_directive(self, **kwargs):
        base = research_plan()
        plan = replace(
            base,
            max_search_queries=4,
            max_pages_read=6,
            max_signals=6,
            max_hypotheses=3,
            queries=(
                *base.queries,
                replace(base.queries[0], query="US industrial hinges importer"),
            ),
        )
        return json.dumps(
            {
                "plan": asdict(plan),
                "interpretation_summary": "受控 UI 合成提案：只研究美国铰链的三线路候选，尚不构成采购确认。",
                "no_auto_send": True,
                "expected_behavior_changes": [
                    "读取公开网页并保存来源证据",
                    "不补全联系人、不验证邮箱、不发送、不报价",
                ],
            }
        )

    async def submit_discovery_proposal(
        self, tenant, raw, plan, summary, changes, model
    ):
        assert tenant == TENANT
        parsed = {}
        for key, value in asdict(plan).items():
            parsed[key] = (
                json.dumps(value)
                if key == "queries"
                else ", ".join(value)
                if isinstance(value, tuple)
                else str(value)
            )
        proposal = ProposalView(
            new_id("dpr"), raw, summary, changes, parsed, "pending_confirmation", NOW
        )
        self.proposals[proposal.proposal_id] = proposal
        return proposal.proposal_id

    async def get_proposal(self, tenant, proposal_id):
        if tenant != TENANT:
            raise PermissionDenied("受控租户不匹配")
        if proposal_id not in self.proposals:
            raise ValidationError("未找到受控提案")
        return self.proposals[proposal_id]

    async def confirm_proposal(self, tenant, proposal_id, actor):
        proposal = await self.get_proposal(tenant, proposal_id)
        self.proposals[proposal_id] = replace(
            proposal,
            state="confirmed",
            decided_by_id=actor,
            decided_at=NOW,
            decided_by_name="受控老板",
        )
        self.active = SimpleNamespace(
            source_proposal_id=proposal_id, directive_id=new_id("dir")
        )

    async def reject_proposal(self, tenant, proposal_id, actor):
        proposal = await self.get_proposal(tenant, proposal_id)
        self.proposals[proposal_id] = replace(
            proposal, state="rejected", decided_by_id=actor, decided_at=NOW
        )

    async def get_active(self, tenant):
        return self.active

    async def start(self, tenant, workflow_type, subject, context, key):
        proposal = await self.get_proposal(tenant, subject)
        assert proposal.state == "confirmed"
        existing = next(
            (run.run_id for run in self.runs.values() if run.subject_ref == subject),
            None,
        )
        if existing:
            return existing
        run_id = RunId(new_id("run"))
        for index, lane in enumerate(LANES):
            name = ("Synthetic Importer", "Synthetic Directory", "Synthetic Online")[
                index
            ]
            url = f"https://{lane}.example.test/about"
            text = (
                f"We are {name}, a supplier of hinges. We are based in the United States."
                if lane != "distributor"
                else "Business directory of hinge distributors."
            )
            evidence = ResearchEvidence.from_page(
                proposal_id=subject,
                query=f"US hinges {lane}",
                discovery_lane=lane,
                query_country="US",
                query_category="hinges",
                text=text,
                url=url,
            )
            signal = DemandSignalView(
                signal_id=new_id("sig"),
                signal_type="product_launch",
                entity_name=name,
                raw_observation=text,
                possible_need="合成推断：可能需要铰链，应通过沟通验证。"
                if lane != "distributor"
                else None,
                status="captured",
                observed_at=NOW,
                source_type="web_public",
                source_ref=f"fixture:{lane}",
                source_url=url,
                page_hash=str(index + 1) * 64,
                snapshot_artifact_ref=new_id("art"),
                research_evidence=evidence,
            )
            self.signals.append(signal)
            if lane != "distributor":
                self.accounts.append(
                    ProspectAccountView(
                        ProspectAccountId(new_id("acc")),
                        TENANT,
                        name,
                        "US",
                        NOW,
                        website_domain=f"{lane}.example.test",
                        source_signal_refs=(signal.signal_id,),
                    )
                )
        self.runs[run_id] = RunSummaryView(
            run_id=run_id,
            workflow_type="demand_discovery",
            workflow_version=2,
            subject_ref=subject,
            current_step="complete",
            status="completed",
            created_at=NOW,
            last_activity_at=NOW,
            next_poll_at=None,
            retry_count=0,
            last_error=None,
            research=RunResearchView(
                planned_discovery_lanes=LANES,
                discovery_lanes=LANES,
                completion_reason="quota_exhausted",
                stop_reason="quota_exhausted",
                searches_used=4,
                pages_used=3,
                signal_count=3,
                hypothesis_count=0,
                pending_verification_count=1,
                consumed_credits=3,
            ),
        )
        return run_id

    async def snapshot(self):
        return None

    async def find_run(self, tenant, proposal_id):
        assert tenant == TENANT
        return next((run.run_id for run in self.runs.values() if run.subject_ref == proposal_id), None)

    async def list_runs(self, tenant, **kwargs):
        assert tenant == TENANT
        return list(reversed(self.runs.values()))

    async def get_run(self, tenant, run_id, **kwargs):
        assert tenant == TENANT
        return (
            RunDetailView(
                summary=self.runs[run_id],
                steps=(),
                tool_calls=(),
                artifacts=(),
                approvals=(),
            )
            if run_id in self.runs
            else None
        )

    async def list_signals(self, tenant, actor, **kwargs):
        assert tenant == TENANT
        return self.signals

    async def list_hypotheses(self, *args, **kwargs):
        return []

    async def list_needs(self, *args, **kwargs):
        return []

    async def list_clusters(self, *args, **kwargs):
        return []

    async def list_notifications(self, *args, **kwargs):
        return []

    async def list_read_view(self, tenant, **kwargs):
        """新准入卡在旧研究预览中保持显式未配置，而不是返回 503。"""
        assert tenant == TENANT
        return SourcingAdmissionListView(
            policy=SourcingAdmissionPolicyView(status="policy_not_configured"),
            items=(),
        )

    async def list_accounts(self, tenant, **kwargs):
        assert tenant == TENANT
        return self.accounts

    async def get_account_detail(self, tenant, account_id):
        assert tenant == TENANT
        return ProspectAccountDetailView(
            next(
                account for account in self.accounts if account.account_id == account_id
            )
        )

    async def for_accounts(self, tenant, accounts):
        assert tenant == TENANT
        return {
            str(account.account_id): tuple(
                signal
                for signal in self.signals
                if signal.signal_id in account.source_signal_refs
            )
            for account in accounts
        }


fixture = Preview()
app = create_app(
    settings=ApiSettings(tenant_id=TENANT, dev_mode=True, retry_after_seconds=17),
    cors_allowed_origins=("http://127.0.0.1:5184",),
)
dependencies = SimpleNamespace(
    employee_authorizer=Phase1EmployeeAuthorizer(TENANT),
    directives=fixture,
    trade_manager=TradeManagerAgent(
        "controlled-no-network", fixture, None, CredentialMarkerGuard()
    ),
    workflow_engine=fixture,
    research_access=ResearchAccessService(TENANT, fixture, configured=True),
    research_execution=fixture,
    research_evidence=fixture,
    demand_radar=fixture,
    prospecting=fixture,
    run_audit=fixture,
    in_app_notifications=fixture,
    sourcing_admission_application=fixture,
    organization=None,
    compliance=None,
    approvals=None,
)
app.dependency_overrides[get_request_identity] = lambda: _identity("boss")
app.dependency_overrides[get_api_dependencies] = lambda: dependencies
