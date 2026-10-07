"""单位准备HTTP白名单，原文展开始终是独立鉴权动作。"""

from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    MessageId,
    ProspectAccountId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary
from shared.schemas.quote_creation import QuoteTime
from shared.schemas.quote_facts import (
    FactHash,
    FactId,
    NeedFactDTO,
    NeedQuantityPreparationStatus,
    NeedUnitPreparationStatus,
)


class NeedUnitPreparationView(NeedFactDTO):
    """展示当前事实与失效状态，不把历史单位值宣称为报价许可。"""

    need_id: ValidatedNeedId
    account_id: ProspectAccountId
    quantity: int | None
    quantity_fact_hash: FactHash | None
    unit: str | None
    unit_confirmation_id: FactId | None
    quantity_status: NeedQuantityPreparationStatus
    unit_status: NeedUnitPreparationStatus
    quantity_origin: ProvenanceSummary | None
    unit_origin: ProvenanceSummary | None


class NeedUnitConfirmationPublicView(NeedFactDTO):
    """不可变receipt的安全摘要；来源原文及locator需独立授权。"""

    need_id: ValidatedNeedId
    confirmation_id: FactId
    quantity_fact_hash: FactHash
    unit: str
    confirmed_by: EmployeeId
    confirmed_at: QuoteTime
    source_message_id: MessageId
    artifact_id: ArtifactId
    content_hash: FactHash
    observed_at: QuoteTime
    unit_origin: ProvenanceSummary
