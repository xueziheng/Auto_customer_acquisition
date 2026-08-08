"""Gmail 连接器实现骨架。接口语义见同目录 AGENTS.md。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="gmail",
    capabilities=(
        "email.send",
        "email.fetch_replies",
        "email.parse_bounce",
        "email.add_label",
        "dns.check_auth",
    ),
    secret_refs=("GMAIL_OAUTH_TOKEN_REF",),
    rate_limit_note="Gmail API 按用户配额；429 带 retry_after",
    compliance_note="发送必须含退订链接；见 docs/architecture/08-compliance.md",
)


@dataclass(frozen=True)
class BounceEvent:
    """解析出的退信事件。

    ``is_hard`` 判断保守：判断不了的按软处理并标 ``needs_review``——
    误判硬退信会错误抑制有效联系人。
    """

    original_message_ref: str
    is_hard: bool
    needs_review: bool
    raw_artifact_ref: str
    occurred_at: datetime
    dedup_key: str


class GmailConnector:
    manifest = MANIFEST

    async def configure(self, secret_resolver: Any) -> None:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError

    async def send(
        self,
        idempotency_key: str,
        from_identity: str,
        to: str,
        subject: str,
        body: str,
        unsubscribe_url: str,
    ) -> str:
        """发送，返回 message_ref。

        实现要求：
        - 幂等键写入自定义 header；发送前先按 header 搜索，
          查到即返回既有 ref——网络超时后的重试是重复发送的
          最大来源
        - ``unsubscribe_url`` 必填，同时写 List-Unsubscribe header
        - token 不出现在任何日志与异常消息
        """
        raise NotImplementedError

    async def fetch_new_messages(
        self, since_cursor: str | None
    ) -> tuple[list[dict], str]:
        """拉新消息，返回 (messages, next_cursor)。游标持久化由调用方
        负责；重复拉取靠消息 Message-ID 去重（conversations 域）。"""
        raise NotImplementedError

    async def parse_bounce(self, raw_message: dict) -> BounceEvent | None:
        raise NotImplementedError

    async def add_label(self, message_ref: str, label: str) -> None:
        raise NotImplementedError

    async def check_dns_auth(self, domain: str) -> dict[str, bool]:
        """SPF/DKIM/DMARC 校验，供 sending_identity 的认证门禁。"""
        raise NotImplementedError
