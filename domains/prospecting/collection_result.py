"""采集器显式返回证据对象和安全摘要，避免跨任务可变旁路。"""

from dataclasses import dataclass

from .inventory import PublicCandidate
from .projection_diagnostic import ProjectionSummary


@dataclass(frozen=True)
class CollectionResult:
    candidates: tuple[PublicCandidate, ...]
    summary: ProjectionSummary

    def __post_init__(self) -> None:
        if (
            not self.summary.projection_complete
            or len(self.candidates) != self.summary.projected_count
        ):
            raise ValueError("采集结果计数不一致")
