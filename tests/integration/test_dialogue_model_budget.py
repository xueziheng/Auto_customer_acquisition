# ruff: noqa: F811 -- pytest 显式复用持久化夹具
"""分类和规划共用持久provider轮预算；未知/重启/重复不能退费或复派。"""
import pytest
from sqlalchemy import select

from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError
from tests.integration.test_dialogue_source import (  # noqa: F401
    dialogue_source_runtime,
    owned_infrastructure,
    page_runtime,
)


async def test_classification_and_planning_share_bound_provider_budget(dialogue_source_runtime):
    from infra.db.dialogue_execution_tables import DialogueBindingRow
    from infra.db.dialogue_model_budget import SqlDialogueModelBudget
    runtime, source, _ = dialogue_source_runtime
    budget = SqlDialogueModelBudget(runtime['factory'], source.tenant_id)
    classifier = InvocationIdentity(tenant_id=source.tenant_id, user_id=new_id('usr'), employee_id=source.owner_employee_id,
        run_id=new_id('run'), capability='reply_qualification', configuration_version='controlled-v1', sequence=0)
    planner = classifier.model_copy(update={'run_id':new_id('run'), 'capability':'ordinary_dialogue'})
    await budget.reserve(source, classifier, provider_round=0, model='controlled-model', maximum_calls=2)
    restarted = SqlDialogueModelBudget(runtime['factory'], source.tenant_id)
    with pytest.raises(ModelGenerationError):
        await restarted.reserve(source, classifier, provider_round=0, model='controlled-model', maximum_calls=2)
    await restarted.reserve(source, planner, provider_round=0, model='controlled-model', maximum_calls=2)
    with pytest.raises(ModelGenerationError):
        await restarted.reserve(source, planner, provider_round=1, model='controlled-model', maximum_calls=2)
    async with runtime['factory']() as db:
        binding = await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id == source.tenant_id, DialogueBindingRow.binding_id == source.binding_id))
        assert binding.model_calls == 2
