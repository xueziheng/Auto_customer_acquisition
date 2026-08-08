"""全局错误基类。

分类原则：按**调用方该怎么办**分，不按错误发生在哪一层分。

```text
输入错的            → ValidationError      改输入再来
没权限              → PermissionDenied     别重试，告诉用户
被规则拦住          → PolicyViolation      别重试，需要人工决定
外部服务临时坏了    → TransientError       可以重试
状态不对            → InvalidStateTransition  别重试，先看当前状态
```

区分「可重试」和「不可重试」是最要紧的：把 ``PermissionDenied``
当成临时错误去重试，会在日志里刷出几千条一样的失败。
"""

from __future__ import annotations

from collections.abc import Mapping


class TradeOSError(Exception):
    """所有自定义错误的根。

    实现要求：
    - 带结构化上下文（``tenant_id``、相关实体 ID），便于日志排查
    - **不要把密钥、Token、Cookie 放进错误消息**——错误消息会进日志，
      日志会被广泛读取（硬边界 1）
    """

    is_retryable: bool = False

    def __init__(self, message: str, *, context: Mapping[str, str] | None = None) -> None:
        """构造：消息进 Exception；context 存为拷贝，防外部可变引用污染。"""
        super().__init__(message)
        self.context: dict[str, str] = dict(context) if context else {}


class ValidationError(TradeOSError):
    """输入不合法。不可重试。"""


class CurrencyMismatchError(ValidationError):
    """币种不匹配（输入错）。不可重试。

    金额/汇率方向与要求不符时抛此错误，见 ``shared/schemas/money.py``。
    """


class PermissionDenied(TradeOSError):
    """权限不足。不可重试。

    必须记录：谁、想对什么做什么、被哪条规则拒绝。
    只记"权限不足"无法排查配置问题。
    """


class TenantIsolationViolation(TradeOSError):
    """跨租户访问。不可重试。

    **这是严重事故，不是普通错误。** 触发时除抛错外必须写审计事件
    并告警——它意味着某处漏了租户过滤，同类代码可能还有别的漏洞。
    """


class PolicyViolation(TradeOSError):
    """被业务规则或合规规则拦下。不可重试。

    子类见 ``domains/`` 各域与 ``tool_gateway/checks``。
    必须能说清是哪条规则——"被策略拒绝"这种消息对使用者毫无帮助。
    """


class InvalidStateTransition(TradeOSError):
    """非法状态转换。不可重试。

    消息里要带当前状态、目标状态和允许的转换列表。
    """


class SuppressedTargetError(PolicyViolation):
    """目标在抑制名单里。

    退订、投诉、硬退信、人工拉黑都会导致。**全局生效**，
    换 Campaign 或换发件身份都不能绕过。
    """


class UnverifiedContactError(PolicyViolation):
    """联系方式未通过可达性验证（硬边界 6）。

    未验证地址进序列会推高退信率，进而毁掉发件域名信誉——
    这条拦的是连带损失，不只是单封邮件失败。
    """


class IndicativePriceInQuoteError(PolicyViolation):
    """试图用 indicative 价格生成客户可见报价（硬边界 7）。

    抓取的网页价格不是可承诺价格。需要例外时走人工确认并留痕，
    不要在代码里放开这道门禁。
    """


class UnsupportedClaimError(PolicyViolation):
    """模型输出了无证据支撑的断言（硬边界 5）。

    由 ``agent_runtime/guardrails`` 抛出。典型场景：把"这家公司正在
    扩张"直接写成"这家公司需要采购我们的产品"。
    """


class ForbiddenCommitmentError(PolicyViolation):
    """试图自动做出必须人工审批的承诺。

    清单见 ``domains/quotations``：价格、折扣、交期、认证、付款条件、
    独家代理、质量保证。
    """


class ApprovalRequired(TradeOSError):
    """需要人工审批才能继续。不可重试。

    携带 ``approval_id``，调用方据此挂起流程等待
    ``ApprovalDecided`` 事件，不要轮询。
    """


class TransientError(TradeOSError):
    """外部依赖临时失败。**可重试。**"""

    is_retryable = True


class RateLimited(TransientError):
    """被限流。可重试。

    要携带 ``retry_after``；没有它就只能猜等多久，通常猜太短。
    """


class ConnectorError(TradeOSError):
    """连接器调用失败。

    子类需明确 ``is_retryable``：认证失败不可重试（重试也是失败），
    网络超时可重试。
    """


class IdempotencyConflict(TradeOSError):
    """幂等键冲突：这件事已经做过了。不可重试。

    **不是错误处理路径，是正常路径。** 调用方应返回上次的结果，
    而不是报错给用户——重复点"发送"不该发出两封邮件，也不该看到
    一个报错。
    """
