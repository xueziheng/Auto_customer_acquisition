"""Sourcing Case V2 工作流公共入口。"""

from workflows.sourcing_case.flow import (
    WORKFLOW_TYPE,
    build_sourcing_case_definition,
    build_sourcing_case_handlers,
)
from workflows.sourcing_case.ports import OpportunityLinkReader, SourcingNeedReader

__all__ = (
    "WORKFLOW_TYPE",
    "OpportunityLinkReader",
    "SourcingNeedReader",
    "build_sourcing_case_definition",
    "build_sourcing_case_handlers",
)
