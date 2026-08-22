"""承诺域对外 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CommitmentView(BaseModel):
    """承诺中心读取模型；保留原话、提取与人工确认留痕。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    commitment_id: str
    commitment_type: Literal["employee", "customer"]
    owner: str
    action: str
    due_at: datetime
    due_at_uncertain: bool
    source_message_id: str
    verbatim: str
    status: Literal[
        "pending", "waiting_customer", "fulfilled", "overdue", "cancelled"
    ]
    account_id: str | None
    opportunity_id: str | None
    extracted_by: str | None
    confirmed_by: str | None
    confirmed_at: datetime | None
    created_at: datetime
    fulfilled_at: datetime | None
    escalated_at: datetime | None


__all__ = ("CommitmentView",)
