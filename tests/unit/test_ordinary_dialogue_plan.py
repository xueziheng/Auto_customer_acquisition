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


@pytest.mark.parametrize('field', ['product_id', 'name_en', 'category', 'spec_display', 'variant_options', 'description_en'])
@pytest.mark.parametrize('response', [
    '{"kind":"ordinary","question_topics":["quantity"]}',
    '{"kind":"review","review_reason":"insufficient_evidence"}',
])
async def test_all_product_model_inputs_are_guarded_before_any_generation(field, response):
    from agent_runtime.qualification_agent.dialogue_plan import OrdinaryDialoguePlanner
    from shared.schemas.model_invocation import (
        InvocationIdentity,
        ModelResponse,
        ModelUsage,
    )
    product = _formal_product('prd_controlled')
    service, _, _ = _service(products=_Products([product]))
    view = await service.get_ordinary_dialogue_view(TENANT, product.product_id,
        actor=ProductActor('system:test', ProductRole.SYSTEM, TENANT))
    synthetic = 'password: synthetic-value-not-a-real-secret'
    view = replace(view, **{field: [synthetic] if field == 'variant_options' else synthetic})
    calls = []
    class Generator:
        async def generate(self, identity, request):
            calls.append(request)
            return ModelResponse(model='synthetic', text=response,
                usage=ModelUsage(input_tokens=1,cached_input_tokens=0,output_tokens=1))
    with pytest.raises(ValidationError):
        await OrdinaryDialoguePlanner(Generator()).plan(
            InvocationIdentity(tenant_id=TENANT,employee_id='employee',user_id='user',run_id='run',
                capability='ordinary_dialogue',configuration_version='v1',sequence=0),
            model='synthetic',customer_subject='Product inquiry',customer_text='Please tell me more.',
            products=(view,),allowed_questions=('quantity',),max_output_tokens=128)
    assert calls == []


async def test_model_product_projection_omits_unused_image_and_ui_fields():
    from agent_runtime.qualification_agent.dialogue_plan import OrdinaryDialoguePlanner
    from shared.schemas.model_invocation import (
        InvocationIdentity,
        ModelResponse,
        ModelUsage,
    )
    product = _formal_product('prd_controlled')
    service, _, _ = _service(products=_Products([product]))
    view = await service.get_ordinary_dialogue_view(TENANT, product.product_id,
        actor=ProductActor('system:test', ProductRole.SYSTEM, TENANT))
    view = replace(view, image_refs=['password: synthetic-omitted-reference'])
    class Generator:
        async def generate(self, identity, request):
            projected=request.payload['customer_visible_products'][0]
            assert set(projected)=={'product_id','name_en','category','spec_display','variant_options','description_en'}
            assert 'synthetic-omitted-reference' not in str(request.payload)
            return ModelResponse(model='synthetic',text='{"kind":"review","review_reason":"insufficient_evidence"}',
                usage=ModelUsage(input_tokens=1,cached_input_tokens=0,output_tokens=1))
    result = await OrdinaryDialoguePlanner(Generator()).plan(
        InvocationIdentity(tenant_id=TENANT,employee_id='employee',user_id='user',run_id='run',capability='ordinary_dialogue',configuration_version='v1',sequence=0),
        model='synthetic',customer_subject='Product inquiry',customer_text='Please tell me more.',products=(view,),allowed_questions=('quantity',),max_output_tokens=128)
    assert result.kind=='review'
