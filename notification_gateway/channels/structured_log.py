"""S3-9 结构化日志通知渠道：固定 logger + 固定结构化键。

设计要点：
- **固定 logger 与固定 keys**：模块 logger（``notification_gateway.channels.
  structured_log``）+ 固定 extra 字段（tenant_id/recipient/priority/dedup_key/
  source_event/title/context/next_step/due_at/link），便于日志采集与告警。
- **相对深链才落日志**：``link`` 必须是相对路径（``/`` 开头且非 ``//``），绝对外链/
  协议相对链以 ``PolicyViolation`` 拒绝；相对链接的 query（含 token）与 fragment
  一律剥离后再落日志。
- **凭证形态内容拒绝**：一切会落日志的自由文本（标题、context 的 key 与 value、
  next_step、链接）中命中高置信凭证形态（DSN userinfo、AKIA、PEM 私钥头、ghp_/
  sk-/xox token、password 赋值含引号包裹等）时以 ``PolicyViolation`` 拒绝，且
  **拒绝时不落任何日志**——日志会被广泛读取（硬边界 1）。password 赋值形态与
  ``scripts/scan_sensitive.py`` 同纪律（允许引号包裹的值）。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlsplit

from notification_gateway.models import Notification
from shared.errors import PolicyViolation

logger = logging.getLogger(__name__)

# 高置信凭证形态（与 scripts/scan_sensitive.py 同纪律：只匹配高置信形态）。
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),  # DSN userinfo
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),  # PEM 私钥头
    re.compile(r"\bgh[psu]_[A-Za-z0-9]{36}\b"),  # GitHub token
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI token
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack token
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(
        r"(?i)password\s*=\s*['\"]?[^\s'\"`\\=,;，。；、：！？…（）【】《》]+"
    ),  # password 赋值（含引号包裹，与 scan_sensitive.py 同纪律）
)


def _contains_credential_shape(text: str) -> bool:
    """text 是否命中高置信凭证形态（含 password 赋值、sk-/AKIA/token 等）。"""
    return any(p.search(text) is not None for p in _CREDENTIAL_PATTERNS)


class StructuredLogChannel:
    """把通知写为结构化日志记录（Phase 1 兜底渠道）。

    实现 ``NotificationChannel``；``deliver`` 先校验，违规抛 ``PolicyViolation`` 且
    不落日志，通过后 ``logger.info`` 落一条固定 keys 的记录。
    """

    name = "structured_log"

    async def deliver(self, notification: Notification) -> None:
        """校验后写一条结构化日志记录；违规（外链/凭证）抛 PolicyViolation。"""
        self._validate(notification)
        logger.info("通知投递（structured_log）", extra=self._log_fields(notification))

    @staticmethod
    def _validate(notification: Notification) -> None:
        """校验所有会落日志的自由字符串；拒绝路径的错误固定且不回显输入。"""
        values = [
            notification.title,
            notification.source_event,
            notification.dedup_key,
            *notification.context.keys(),
            *notification.context.values(),
        ]
        if notification.next_step is not None:
            values.append(notification.next_step)
        if notification.link is not None:
            values.append(notification.link)
        if any(_contains_credential_shape(value) for value in values):
            raise PolicyViolation("通知字段含凭证形态内容，拒绝写入日志")
        if notification.link is not None:
            StructuredLogChannel._clean_relative_link(notification.link)

    @staticmethod
    def _clean_relative_link(link: str) -> str:
        """规范化相对深链为无 query/fragment 的路径，拒绝外链和浏览器歧义路径。"""
        try:
            parsed = urlsplit(link)
        except ValueError:
            raise PolicyViolation("只接受规范化后的相对深链") from None
        path = parsed.path
        normalized_path = unquote(path)
        if (
            parsed.scheme
            or parsed.netloc
            or not path.startswith("/")
            or path.startswith("//")
            or "\\" in link
            or "\\" in normalized_path
            or normalized_path.startswith("//")
            or any(ord(char) < 32 or ord(char) == 127 for char in link)
            or any(ord(char) < 32 or ord(char) == 127 for char in normalized_path)
        ):
            raise PolicyViolation("只接受规范化后的相对深链")
        return path

    @staticmethod
    def _log_fields(notification: Notification) -> dict[str, object]:
        """构造固定结构化字段；``link`` 已剥离 query 与 fragment。"""
        link = (
            StructuredLogChannel._clean_relative_link(notification.link)
            if notification.link is not None
            else None
        )
        return {
            "tenant_id": str(notification.tenant_id),
            "recipient": str(notification.recipient),
            "priority": notification.priority.value,
            "dedup_key": notification.dedup_key,
            "source_event": notification.source_event,
            "title": notification.title,
            "context": notification.context,
            "next_step": notification.next_step,
            "due_at": (
                notification.due_at.isoformat()
                if notification.due_at is not None
                else None
            ),
            "link": link,
        }
