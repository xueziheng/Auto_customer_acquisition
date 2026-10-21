"""普通计划只选择可见事实和最多两个缺项；不能发明对外句子或商业承诺。"""
from dataclasses import replace

import pytest
from pydantic import ValidationError as SchemaError

from domains.products.permissions import ProductActor, ProductRole
from shared.errors import ValidationError
from tests.unit.test_product_service import TENANT, _formal_product, _Products, _service


async def test_ordinary_plan_renders_only_exact_public_product_fields_and_allowed_questions():
    from agent_runtime.qualification_agent.dialogue_plan import (
        DialoguePlan,
        render_dialogue,
    )
    product = _formal_product('prd_controlled')
    product.spec_summary = 'Steel hinges for cabinet doors.'
    service, _, _ = _service(products=_Products([product]))
    view = await service.get_ordinary_dialogue_view(TENANT, product.product_id, actor=ProductActor('system:test', ProductRole.SYSTEM, TENANT))
    plan = DialoguePlan.model_validate({'kind':'ordinary', 'facts':[{'product_id':view.product_id,'section':'spec_display'}], 'question_topics':['application','quantity']})
    body = render_dialogue(plan, (view,), allowed_questions=('application','quantity'))
    assert view.spec_display in body and 'intended application' in body and 'approximate quantity' in body
    assert 'internal' not in body and body.isascii()
    with pytest.raises(SchemaError):
        DialoguePlan.model_validate({'kind':'ordinary','body':'Price is USD 10','facts':[],'question_topics':[]})
    with pytest.raises(ValidationError):
        render_dialogue(plan, (replace(view, spec_display='Guaranteed delivery next week'),), allowed_questions=('application','quantity'))
    with pytest.raises(ValidationError):
        render_dialogue(plan, (view,), allowed_questions=('quantity',))


async def test_public_english_specs_accept_units_punctuation_but_not_hidden_control_or_commercial_terms():
    from agent_runtime.qualification_agent.dialogue_plan import (
        DialoguePlan,
        render_dialogue,
    )
    product = _formal_product('prd_controlled')
    service, _, _ = _service(products=_Products([product]))
    view = await service.get_ordinary_dialogue_view(TENANT, product.product_id, actor=ProductActor('system:test', ProductRole.SYSTEM, TENANT))
    plan = DialoguePlan.model_validate({'kind':'ordinary', 'facts':[{'product_id':view.product_id,'section':'spec_display'}]})
    for text in ['Diameter Ø 10 × 20 mm; tolerance ± 0.1 mm; angle 90°.', 'Buyer’s choice: polished finish.']:
        assert text in render_dialogue(plan, (replace(view, spec_display=text),), allowed_questions=())
    for text in ['Hidden \u202e text', '价格保证', 'Price € 10', 'Guaranteed stock']:
        with pytest.raises(ValidationError):
            render_dialogue(plan, (replace(view, spec_display=text),), allowed_questions=())
