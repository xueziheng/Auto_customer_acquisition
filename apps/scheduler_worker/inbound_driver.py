"""原 singleton 的单页入站阶段；不拥有循环、锁或资源生命周期。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from apps.composition_support.email_inbound import InboundComposition
from shared.errors import TradeOSError
from shared.schemas.email_inbound import InboundError
from tool_gateway.errors import ToolGatewayError
from workflows.reply_qualification.inbound_contracts import InboundPageError


class InboundDriver:
    def __init__(
        self, composition: InboundComposition, *, now: Callable[[], datetime]
    ) -> None:
        self._composition = composition
        self._now = now

    async def scan_once(self) -> int:
        """持久绑定后才读取；暂态到期原位重试，永久失败等待人工核对。"""
        c = self._composition
        current = await c.store.read_cursor()
        if current is None:
            return 0
        if current.blocked_reason is not None and (
            current.next_retry_at is None or current.next_retry_at > self._now()
        ):
            return 0
        try:
            page = await c.reader_for(current.route).fetch(
                current.route.tenant_id, current.route.mailbox_alias, current.cursor, 20
            )
            await c.processor.process(current, page)
            return 1
        except ToolGatewayError as error:
            reason = error.category.value
            if reason not in {
                "rate_limited",
                "provider_transient",
                "provider_auth_required",
                "provider_permanent",
            }:
                reason = (
                    "provider_transient" if error.is_retryable else "provider_permanent"
                )
            delay = (
                max(1, min(86400, error.retry_after_seconds or 30))
                if error.is_retryable
                else None
            )
        except InboundPageError as error:
            if error.reason == "cursor_conflict":
                return 0
            reason = (
                error.reason
                if error.reason in {"receipt_conflict", "page_integrity"}
                else "storage_unavailable"
            )
            delay = 30 if reason == "storage_unavailable" else None
        except InboundError:
            reason, delay = "page_integrity", None
        except TradeOSError:
            reason, delay = "domain_rejected", None
        except Exception:  # noqa: BLE001 - 仅固化固定类别，不保留异常原文
            reason, delay = "storage_unavailable", 30
        try:
            await c.store.mark_failure(
                current,
                reason=reason,
                next_retry_at=self._now() + timedelta(seconds=delay) if delay else None,
            )
        except InboundPageError as error:
            if error.reason != "cursor_conflict":
                raise
        return 0
