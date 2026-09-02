"""Sourcing Case V2 Workflow 的最小、确定性上下文构造。"""

from domains.sourcing.schemas import SourcingNeedSnapshot
from workflows.sourcing_case.application import sourcing_case_start_context


def _safe_context(snapshot: SourcingNeedSnapshot, case_id: str) -> dict[str, object]:
    """只从可信 Need snapshot 生成既有 V2 context，禁止补造业务事实。"""

    return sourcing_case_start_context(snapshot, case_id)


__all__ = ()
