"""来源验收的结果分类不能把空结果或局部来源称为三线路验收成功。"""

from types import SimpleNamespace

from apps.scheduler_worker.research_acceptance import SourceAcceptanceStep
from shared.schemas.identifiers import UserId, new_id
from tests.integration.test_search_quota import NOW
from tests.unit.workflows.test_research_discovery import research_plan
from tool_gateway.handlers.web_slots import SearchResultBatch
from workflows.engine.runner import StepStatus, WorkflowRun


async def test_empty_source_acceptance_does_not_report_pages_only_success():
    class Reader:
        async def load_confirmed(self, *args):
            return research_plan()

    class Search:
        async def search(self, tenant, run, query, country, category, limit):
            return SearchResultBatch(new_id("wsb"), tenant, country, category, ())

        def release(self, batch):
            pass

        def discard_all(self):
            pass

    run = WorkflowRun(
        new_id("run"),
        new_id("tn"),
        "research_source_acceptance",
        1,
        "proposal:test",
        "execute_search",
        StepStatus.RUNNING,
        NOW,
        context={
            "proposal_id": "proposal:test",
            "acting_user_id": str(UserId(new_id("emp"))),
        },
    )
    _, _, result = await SourceAcceptanceStep(
        Reader(), SimpleNamespace(searcher=Search())
    ).execute(run)
    assert result["completion_reason"] == "no_results"
    assert result["pages"] == []
