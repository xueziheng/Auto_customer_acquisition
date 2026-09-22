"""产品域特有错误。"""

from __future__ import annotations

from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PolicyViolation,
    ValidationError,
)


class ProductNotFoundError(ValidationError):
    """同租户缺失与跨租户隐藏统一的安全产品缺失结果。"""


class ViewLeakError(PolicyViolation):
    """检测到内部字段流向外部视图的序列化路径。

    防御性检查：客户视图序列化前断言不含 supplier / cost 字段。
    内部成本泄漏给客户是永久性商业损伤——以后每次谈判都从你的
    底价开始。
    """


class CandidateNotVerifiedError(PolicyViolation):
    """候选产品未完成逐项确认就试图升级为正式产品。"""


class CatalogPolicyNotFoundError(ProductNotFoundError):
    """同租户缺失与跨租户隐藏统一的目录提案策略缺失结果。"""


class CatalogPolicyIdempotencyConflictError(IdempotencyConflict):
    """策略创建幂等键已绑定不同不可变请求。"""


class CatalogPolicyApprovalConflictError(IdempotencyConflict):
    """策略审批 ID 或请求摘要已绑定不同不可变 subject。"""


class CatalogPolicyDecisionInvalidError(PolicyViolation):
    """workflow 提供的中央审批事实与策略 subject 不精确匹配。"""


class CatalogPolicyStateTransitionError(InvalidStateTransition):
    """策略已处于不允许应用当前决定的终态。"""


class CatalogEvaluationNotFoundError(ProductNotFoundError):
    """同租户缺失与跨租户隐藏统一的目录评估缺失结果。"""


class CatalogEvaluationConflictError(IdempotencyConflict):
    """评估唯一 subject 已绑定不同的快照或系统 Run。"""


class CatalogProposalNotFoundError(ProductNotFoundError):
    """同租户缺失与跨租户隐藏统一的目录产品提案缺失结果。"""


class CatalogProposalApprovalConflictError(IdempotencyConflict):
    """提案审批 ID、请求摘要或持久化 subject 不精确匹配。"""


class CatalogProposalDecisionInvalidError(PolicyViolation):
    """workflow 提供的中央审批事实与提案 subject 不精确匹配。"""


class CatalogProposalStateTransitionError(InvalidStateTransition):
    """提案已处于不允许应用当前决定的终态。"""


class CatalogCultivationCaseNotFoundError(ProductNotFoundError):
    """同租户缺失与跨租户隐藏统一的培养 Case 缺失结果。"""


class CatalogCultivationConflictError(IdempotencyConflict):
    """培养 Case 唯一 subject 已绑定不同不可变内容。"""


__all__ = (
    "CandidateNotVerifiedError",
    "CatalogCultivationCaseNotFoundError",
    "CatalogCultivationConflictError",
    "CatalogEvaluationConflictError",
    "CatalogEvaluationNotFoundError",
    "CatalogPolicyApprovalConflictError",
    "CatalogPolicyDecisionInvalidError",
    "CatalogPolicyIdempotencyConflictError",
    "CatalogPolicyNotFoundError",
    "CatalogPolicyStateTransitionError",
    "CatalogProposalApprovalConflictError",
    "CatalogProposalDecisionInvalidError",
    "CatalogProposalNotFoundError",
    "CatalogProposalStateTransitionError",
    "ProductNotFoundError",
    "ViewLeakError",
)
