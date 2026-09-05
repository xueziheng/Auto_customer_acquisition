"""进程组合能力的安全状态；不表示已通过商业门禁或完成外部动作。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

CapabilityName = Literal[
    "research",
    "contacts",
    "campaign",
    "reply",
    "sourcing",
    "quotation",
    "inbound_body",
    "full_reply",
    "agent",
    "browser",
]
CapabilityState = Literal["disabled", "enabled", "configuration_error"]
CapabilityReason = Literal[
    "not_requested",
    "composed",
    "required_ports_missing",
    "not_implemented",
    "worker_required",
]


class RuntimeCapability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: CapabilityName
    status: CapabilityState
    reason: CapabilityReason
