"""强类型 ID。

为什么不用裸 ``str``：把 ``need_id`` 传进期望 ``opportunity_id`` 的参数，
用裸 str 时类型检查器不会报错，而这类 bug 在运行时表现为"查不到数据"，
排查成本很高。``NewType`` 让静态检查能拦下来，运行时零开销。

ID 格式约定：``<前缀>_<唯一部分>``，例如 ``opp_01H2X...``。
前缀便于日志排查和跨系统引用——看到 ``need_xxx`` 就知道该查哪张表。
"""

import secrets
import time
from typing import NewType

from shared.errors import ValidationError

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
OutboundMessageId = NewType("OutboundMessageId", str)
"""RFC 5322 出站 Message-ID（= outreach attempt 的 deterministic_message_id，
投递关联键）。与入站 MessageId 是不同命名空间，不得混用。"""

# --- 触达 ---------------------------------------------------------------

CampaignId = NewType("CampaignId", str)
SequenceId = NewType("SequenceId", str)
EnrollmentId = NewType("EnrollmentId", str)
MessageAttemptId = NewType("MessageAttemptId", str)
SuppressionId = NewType("SuppressionId", str)
SendingIdentityId = NewType("SendingIdentityId", str)

# --- 机会与供应 ---------------------------------------------------------

OpportunityId = NewType("OpportunityId", str)
ScoreSnapshotId = NewType("ScoreSnapshotId", str)
LossRecordId = NewType("LossRecordId", str)
ProductId = NewType("ProductId", str)
ProductVariantId = NewType("ProductVariantId", str)
SupplierId = NewType("SupplierId", str)
SourcingCaseId = NewType("SourcingCaseId", str)
SupplierCandidateId = NewType("SupplierCandidateId", str)
SourcingPlanId = NewType("SourcingPlanId", str)
SourcingReviewId = NewType("SourcingReviewId", str)
SourcingSupplyOptionId = NewType("SourcingSupplyOptionId", str)

# --- 成本与报价 ---------------------------------------------------------

CostSheetId = NewType("CostSheetId", str)
QuoteId = NewType("QuoteId", str)
QuoteFileId = NewType("QuoteFileId", str)
PriceSnapshotId = NewType("PriceSnapshotId", str)
FxSnapshotId = NewType("FxSnapshotId", str)

# --- 协作 ---------------------------------------------------------------

CommitmentId = NewType("CommitmentId", str)
WorkUploadId = NewType("WorkUploadId", str)
WorkExtractionId = NewType("WorkExtractionId", str)
EmployeeConfirmationId = NewType("EmployeeConfirmationId", str)
TaskId = NewType("TaskId", str)
HandoffId = NewType("HandoffId", str)
DirectiveId = NewType("DirectiveId", str)
ApprovalId = NewType("ApprovalId", str)
ChangeSetId = NewType("ChangeSetId", str)
PlaybookVersionId = NewType("PlaybookVersionId", str)
PlaybookActivationId = NewType("PlaybookActivationId", str)
CountryPolicyVersionId = NewType("CountryPolicyVersionId", str)
"""不可变国家政策候选版本 ID；值使用 ``cpp_`` 前缀。"""
CountryPolicyActivationId = NewType("CountryPolicyActivationId", str)
"""append-only 国家政策激活事实 ID；值使用 ``cpa_`` 前缀。"""
NotificationJobId = NewType("NotificationJobId", str)
NotificationId = NewType("NotificationId", str)
AuthenticationCheckRequestId = NewType("AuthenticationCheckRequestId", str)

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


# Crockford base32 字符集（ULID 标准，不含 I/L/O/U）。
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_ulid(ts_ms: int, rand_bytes: bytes) -> str:
    """把 48-bit 毫秒时间戳 + 80-bit 随机编码为 26 字符 Crockford base32（ULID）。

    私有纯函数：位/字节边界属编程错误，抛内置 ``ValueError``。
    """
    if ts_ms < 0 or ts_ms >= 2**48:
        raise ValueError("ts_ms 必须满足 0 <= ts_ms < 2^48")
    if len(rand_bytes) != 10:
        raise ValueError("rand_bytes 必须恰为 10 字节（80 位）")
    value = (ts_ms << 80) | int.from_bytes(rand_bytes, "big")
    chars = [_CROCKFORD[(value >> (5 * i)) & 0x1F] for i in range(25, -1, -1)]
    return "".join(chars)


def new_id(prefix: str) -> str:
    """生成带前缀的 ID。

    实现要求：
    - 用时间有序的方案（ULID 或 UUIDv7），便于按主键范围扫描和分页
    - 不要用自增整数：会泄露业务量，且多租户下不便合并
    - 前缀与实体一一对应，见本模块各 ID 类型的命名
    - prefix strip 后非空；空白前缀是调用方错误（ValidationError）
    """
    cleaned = prefix.strip()
    if not cleaned:
        raise ValidationError("prefix 不能为空")
    ts_ms = time.time_ns() // 1_000_000
    ulid = _encode_ulid(ts_ms, secrets.token_bytes(10))
    return f"{cleaned}_{ulid}"
