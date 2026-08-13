"""Artifact Store 的固定、脱敏错误。"""

from shared.errors import TradeOSError


class ArtifactConflictError(TradeOSError):
    """相同幂等键绑定了不同的派生产物。"""

    def __init__(self) -> None:
        super().__init__("Artifact 幂等记录冲突")


class ArtifactNotFoundError(TradeOSError):
    """不存在与跨租户访问共用的不可枚举错误。"""

    def __init__(self) -> None:
        super().__init__("Artifact 不存在")


class ArtifactIntegrityError(TradeOSError):
    """对象的长度或 SHA-256 与已提交元数据不一致。"""

    def __init__(self) -> None:
        super().__init__("Artifact 完整性校验失败")
