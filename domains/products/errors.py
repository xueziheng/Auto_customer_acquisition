"""产品域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


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
