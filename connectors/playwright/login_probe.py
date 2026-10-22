"""只调用隔离服务的固定登录恢复动作；本适配器不运行浏览器或读取凭证。"""
import json
from datetime import UTC, datetime

import httpx

from shared.schemas.browser_probe import BrowserProbeObservation, BrowserProbeOutcome


def parse_login_probe(payload: object) -> BrowserProbeOutcome:
    """只提取严格布尔和固定状态，未知响应不能变成成功或会话过期。"""
    if not isinstance(payload, dict) or payload.get("environment") != "aliyun-server":
        return "unavailable"
    status = payload.get("http_status")
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "blocked"
    if payload.get("error_kind") or type(status) is not int or not 200 <= status < 300:
        return "unavailable"
    if payload.get("authenticated") is True and payload.get("restored") is True and payload.get("saved") is True:
        return "available"
    # 未确认登录不能一概称为过期；只有确实跳到登录页才要求重新登录。
    if payload.get("login_required") is True:
        return "login_required"
    return "unavailable"


class UngmLoginProbeConnector:
    """固定 loopback 入口，不跟随重定向、不接受客户端 URL、无自动重试。"""
    async def check(self) -> BrowserProbeObservation:
        outcome: BrowserProbeOutcome = "unavailable"
        try:
            async with httpx.AsyncClient(timeout=55, follow_redirects=False, trust_env=False) as client:  # noqa: SIM117
                async with client.stream(
                    "POST", "http://127.0.0.1:8791/action/reopen",
                    headers={"Origin": "http://127.0.0.1:8791", "X-TradeOS-Pilot": "1"},
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 16384:
                            return BrowserProbeObservation(outcome="unavailable", checked_at=datetime.now(UTC))
                        body.extend(chunk)
                    if response.status_code == 200:
                        outcome = parse_login_probe(json.loads(body))
                    elif response.status_code == 429:
                        outcome = "rate_limited"
        except (httpx.HTTPError, ValueError, TypeError):
            outcome = "unavailable"
        return BrowserProbeObservation(outcome=outcome, checked_at=datetime.now(UTC))
