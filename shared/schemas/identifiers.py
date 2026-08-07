"""强类型 ID。

为什么不用裸 ``str``：把 ``need_id`` 传进期望 ``opportunity_id`` 的参数，
用裸 str 时类型检查器不会报错，而这类 bug 在运行时表现为"查不到数据"，
排查成本很高。``NewType`` 让静态检查能拦下来，运行时零开销。

ID 格式约定：``<前缀>_<唯一部分>``，例如 ``opp_01H2X...``。
前缀便于日志排查和跨系统引用——看到 ``need_xxx`` 就知道该查哪张表。
"""

from typing import NewType

# --- 租户与身份 ---------------------------------------------------------

TenantId = NewType("TenantId", str)
"""租户 ID。所有业务数据都归属于某个租户（硬边界 8）。

Phase 1 单租户期间是恒定值，但**不得省略**：省略它等于把多租户改造
推给未来，而那次改造的失败模式是静默的跨租户数据泄露。
"""

UserId = NewType("UserId", str)
EmployeeId = NewType("EmployeeId", str)
TeamId = NewType("TeamId", str)

# --- 需求四层 -----------------------------------------------------------

DemandSignalId = NewType("DemandSignalId", str)
NeedHypothesisId = NewType("NeedHypothesisId", str)
ValidatedNeedId = NewType("ValidatedNeedId", str)
NeedClusterId = NewType("NeedClusterId", str)

# --- 客户开发与 CRM -----------------------------------------------------

ProspectAccountId = NewType("ProspectAccountId", str)
ProspectContactId = NewType("ProspectContactId", str)
ContactPointId = NewType("ContactPointId", str)
CompanyId = NewType("CompanyId", str)
ContactId = NewType("ContactId", str)
ConversationId = NewType("ConversationId", str)
MessageId = NewType("MessageId", str)

# --- 触达 ---------------------------------------------------------------

CampaignId = NewType("CampaignId", str)
SequenceId = NewType("SequenceId", str)
EnrollmentId = NewType("EnrollmentId", str)
MessageAttemptId = NewType("MessageAttemptId", str)
SendingIdentityId = NewType("SendingIdentityId", str)

# --- 机会与供应 ---------------------------------------------------------

OpportunityId = NewType("OpportunityId", str)
ProductId = NewType("ProductId", str)
ProductVariantId = NewType("ProductVariantId", str)
SupplierId = NewType("SupplierId", str)
SourcingCaseId = NewType("SourcingCaseId", str)
SupplierCandidateId = NewType("SupplierCandidateId", str)

# --- 成本与报价 ---------------------------------------------------------

CostSheetId = NewType("CostSheetId", str)
QuoteId = NewType("QuoteId", str)
PriceSnapshotId = NewType("PriceSnapshotId", str)
FxSnapshotId = NewType("FxSnapshotId", str)

# --- 协作 ---------------------------------------------------------------

CommitmentId = NewType("CommitmentId", str)
TaskId = NewType("TaskId", str)
HandoffId = NewType("HandoffId", str)
DirectiveId = NewType("DirectiveId", str)
ApprovalId = NewType("ApprovalId", str)
ChangeSetId = NewType("ChangeSetId", str)

# --- Agent 运行 ---------------------------------------------------------

RunId = NewType("RunId", str)
StepId = NewType("StepId", str)
ToolCallId = NewType("ToolCallId", str)
ArtifactId = NewType("ArtifactId", str)
SkillId = NewType("SkillId", str)

# --- 幂等 ---------------------------------------------------------------

IdempotencyKey = NewType("IdempotencyKey", str)
"""幂等键。

语义上等于"这件事"，而不是"这次调用"。推荐构造方式：
``{tenant_id}:{tool_id}:{业务实体 id}:{步骤序号}``

必须持久化到数据库，不能只放 Redis——Redis 丢键等于重复发送邮件。
"""


def new_id(prefix: str) -> str:
    """生成带前缀的 ID。

    实现要求：
    - 用时间有序的方案（ULID 或 UUIDv7），便于按主键范围扫描和分页
    - 不要用自增整数：会泄露业务量，且多租户下不便合并
    - 前缀与实体一一对应，见本模块各 ID 类型的命名
    """
    raise NotImplementedError
