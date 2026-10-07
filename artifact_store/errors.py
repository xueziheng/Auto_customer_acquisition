"""Artifact Store 的固定、脱敏错误。"""

from shared.errors import TradeOSError, TransientError


class ArtifactBoundedReadUnavailable(TradeOSError):
    """旧Store没有专用有界能力时固定拒绝，禁止全量回退。"""

    def __init__(self) -> None:
        super().__init__("Artifact 有界读取不可用")


class ArtifactReadLimitExceeded(TradeOSError):
    """元数据或实际对象超过本次读取上限。"""

    def __init__(self) -> None:
        super().__init__("Artifact 超过读取限制")


class ArtifactCommitUnknownError(TransientError):
    """PDF对象尝试写入后结果未知，原key可核对但不得删除candidate。"""

    code = "artifact_commit_unknown"

    def __init__(self) -> None:
        super().__init__("Artifact 提交状态未知")


class ArtifactUnavailableError(TransientError):
    """新增PDF与安全key读取的固定依赖故障，不携带SQL或对象键。"""

    code = "artifact_unavailable"

    def __init__(self) -> None:
        super().__init__("Artifact 暂不可用")


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
