"""确定性投递报告元数据；只能由完整MIME解析产生，绝不来自模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DialogueDeliveryReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)
    kind: Literal['hard_bounce', 'soft_bounce', 'complaint']
    original_message_id: str = Field(repr=False, max_length=256)
    original_idempotency_header: str = Field(repr=False, max_length=128)
    original_sender: str = Field(repr=False, max_length=254)
    original_recipient: str = Field(repr=False, max_length=254)
    occurred_at: datetime
