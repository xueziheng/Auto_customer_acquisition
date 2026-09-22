"""固定安全错误；不回显输入或其他员工的轮次身份。"""

from shared.errors import TradeOSError


class AssistantNotFound(TradeOSError):
    def __init__(self) -> None:
        super().__init__("会话或轮次不可见")


class AssistantConflict(TradeOSError):
    def __init__(self) -> None:
        super().__init__("请求与当前会话状态冲突，请读取已有轮次")
