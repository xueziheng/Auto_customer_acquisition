"""普通对话表单只收业务选择；产品可见视图与模型硬上限由服务端绑定。"""
from dataclasses import asdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import ConfiguredApiDependencies
from apps.api.identity import RequestIdentity
from domains.outreach.ordinary_dialogue_policy import (
    DialogueProductRef,
    OrdinaryDialoguePolicyInput,
)
from domains.products.permissions import ProductActor, ProductRole
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, ProductId, SendingIdentityId
from shared.schemas.quote_facts import canonical_fact_hash


class OrdinaryDialogueSelection(BaseModel):
    """企业选择产品、实际负责人、邮箱及业务轮次，不提交hash或运行参数。"""
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    owner_employee_id: EmployeeId
    assessment_ref: str = Field(min_length=1, max_length=512)
    reply_sender_identity_ids: list[SendingIdentityId] = Field(min_length=1, max_length=5)
    product_ids: list[ProductId] = Field(min_length=1, max_length=5)
    expires_at: datetime = Field(strict=False)
    max_rounds: int = Field(ge=1, le=10)


async def bind_dialogue_selection(selection: OrdinaryDialogueSelection | None,
    identity: RequestIdentity, dependencies: ConfiguredApiDependencies) -> OrdinaryDialoguePolicyInput | None:
    """调用方已核Campaign管理权；这里只读取当前FORMAL、获准外用的客户投影。"""
    if selection is None:
        return None
    if dependencies.products is None or len(set(selection.product_ids)) != len(selection.product_ids):
        raise ValidationError("普通对话产品选择无效")
    refs = []
    for product_id in selection.product_ids:
        visible = await dependencies.products.get_ordinary_dialogue_view(identity.tenant_id, product_id,
            actor=ProductActor(str(identity.employee.employee_id), ProductRole.BOSS, identity.tenant_id))
        refs.append(DialogueProductRef(product_id=str(product_id), customer_view_sha256=canonical_fact_hash(asdict(visible))))
    return OrdinaryDialoguePolicyInput(owner_employee_id=selection.owner_employee_id,
        assessment_ref=selection.assessment_ref, reply_sender_identity_ids=tuple(selection.reply_sender_identity_ids),
        product_refs=tuple(refs), expires_at=selection.expires_at, max_rounds=selection.max_rounds,
        max_model_calls=min(40, selection.max_rounds * 4), max_no_progress_rounds=2,
        max_input_bytes=32768, max_output_tokens=2048)
