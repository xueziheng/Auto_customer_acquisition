"""原 singleton 的单页入站阶段；不拥有循环、锁或资源生命周期。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from apps.composition_support.email_inbound import InboundComposition
from shared.errors import TradeOSError
from shared.schemas.email_inbound import InboundError
from shared.schemas.qq_trial import QQTrialStopped
from tool_gateway.errors import ToolGatewayError
from workflows.reply_qualification.inbound_contracts import InboundPageError

logger = logging.getLogger(__name__)


async def _stop_trial(trial, reason: str) -> None:
    try:
        await asyncio.shield(trial.stop(reason))
    except BaseException:  # noqa: BLE001 - 停止审计失败不能遮蔽原取消/未知结果
        logger.error("QQ 验收停止记录失败；保留持久预留")


class InboundDriver:
    def __init__(
        self, composition: InboundComposition, *, now: Callable[[], datetime], poll_interval_seconds: int = 0
    ) -> None:
        self._composition = composition
        self._now = now
        self._interval = timedelta(seconds=poll_interval_seconds)

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
        if current.blocked_reason is None and current.last_succeeded_at is not None and (
            self._now() < current.last_succeeded_at + self._interval
        ):
            return 0
        trial = getattr(c, "trial_gate", None)
        try:
            if trial is not None and not await trial.claim_scan():
                return 0
            page = await c.reader_for(current.route).fetch(
                current.route.tenant_id, current.route.mailbox_alias, current.cursor, 20
            )
            await c.processor.process(current, page)
            return 1
        except QQTrialStopped:
            reason, delay = "provider_permanent", None
        except asyncio.CancelledError:
            if trial is not None:
                await _stop_trial(trial, "cancelled")
            raise
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
            if error.reason == "cursor_conflict" and trial is None:
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
        if trial is not None:
            await _stop_trial(trial, "failed_or_unknown")
            delay = None
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
