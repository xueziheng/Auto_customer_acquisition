"""浏览器登录检查的无凭证事实；不包含 Cookie、账号资料或页面正文。"""
from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict

BrowserProbeOutcome = Literal["available", "login_required", "blocked", "rate_limited", "unavailable"]


class BrowserProbeObservation(BaseModel):
    """一次实际检查的固定结果与时间，不推断会话的未来有效期。"""
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    outcome: BrowserProbeOutcome
    checked_at: AwareDatetime


class BrowserProbeConnector(Protocol):
    async def check(self) -> BrowserProbeObservation:
        """只检查受信部署绑定的网站和会话，不接受任意 URL 或凭证。"""
        ...


class BrowserSourceView(BaseModel):
    """过去的检查结果；stale 表示需要复测，不代表登录已经过期。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["not_checked", "available", "stale", "login_required", "blocked", "rate_limited", "unavailable"] = "not_checked"
    checked_at: datetime | None = None
    next_check_at: datetime | None = None
