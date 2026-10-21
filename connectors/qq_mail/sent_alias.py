"""核对邮件头与标记，显式保留来源；解析成功不代表采集成功或运行授权。"""

import hashlib
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesHeaderParser
from typing import Literal

from shared.schemas.qq_trial import QQTrialStopped

AliasEvidenceSource = Literal["sender_sent", "recipient_export", "controlled_recipient_imap"]
_MARKER = re.compile(r"[a-z0-9-]{1,32}\.[0-9a-f]{64}")


def _unfold_token(value: str) -> str:
    """只展开 CRLF+WSP 并去掉外层 SP/HTAB，保留内部空白供语法拒绝。"""
    value = re.sub(r"\r\n(?=[ \t])", "", value).strip(" \t")
    if not _MARKER.fullmatch(value):
        raise QQTrialStopped("alias_evidence_invalid")
    return value


def _strict_header_lines(raw: bytes) -> None:
    """避免宽松邮件解析器吞掉非法换行、孤立折行或字段名前后空白。"""
    if re.search(rb"(?<!\r)\n|\r(?!\n)|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", raw):
        raise QQTrialStopped("alias_evidence_invalid")
    has_field = False
    for line in raw[:-4].split(b"\r\n"):
        if line.startswith((b" ", b"\t")):
            if not has_field or not line.strip(b" \t"):
                raise QQTrialStopped("alias_evidence_invalid")
        elif re.match(rb"[\x21-\x39\x3b-\x7e]+:", line):
            has_field = True
        else:
            raise QQTrialStopped("alias_evidence_invalid")


@dataclass(frozen=True, repr=False)
class SentAliasEvidence:
    source: AliasEvidenceSource
    provider_header: str
    canonical_header: str
    sender: str
    recipient: str
    headers_sha256: str
    raw_headers: bytes


def verify_sent_alias(
    headers: tuple[bytes, ...],
    *,
    source: AliasEvidenceSource,
    canonical_header: str,
    idempotency_header: str,
    sender: str,
    recipient: str,
) -> SentAliasEvidence:
    """来源必须由受信调用者提供，不能从不受信邮件内容推断。

    客户端 APPEND 的原编号副本不提供 QQ 改写编号证据。这里只解析已有受信
    Connector 或收件人导出的头，不搜索邮箱、不从主题/正文生成别名。
    recipient_export 只提供历史核对结果，不能用于 Sent 采集成功门禁。
    """
    if (
        source not in {"sender_sent", "recipient_export", "controlled_recipient_imap"}
        or not 1 <= len(headers) <= 20
        or not _MARKER.fullmatch(idempotency_header)
        or not re.fullmatch(
            r"<[a-z0-9-]{1,32}\.[0-9a-f]{64}@messages\.tradeos\.invalid>",
            canonical_header,
        )
        or (sender, recipient) != ("1249583590@qq.com", "986647123@qq.com")
        or canonical_header != f"<{idempotency_header}@messages.tradeos.invalid>"
    ):
        raise QQTrialStopped("alias_evidence_invalid")
    found: list[SentAliasEvidence] = []
    for raw in headers:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
            raise QQTrialStopped("alias_evidence_invalid")
        separator = re.search(rb"\r?\n\r?\n", raw)
        if separator is None or separator.end() != len(raw):
            raise QQTrialStopped("alias_headers_only")
        if not raw.endswith(b"\r\n\r\n"):
            raise QQTrialStopped("alias_evidence_invalid")
        _strict_header_lines(raw)
        value = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        names = ("Message-ID", "X-TradeOS-Idempotency-V1", "From", "To")
        if (
            value.defects
            or any(len(value.get_all(k, [])) != 1 for k in names)
            or value.get_all("Cc")
            or value.get_all("Bcc")
            or any(getattr(value[k], "defects", ()) for k in names)
        ):
            raise QQTrialStopped("alias_evidence_invalid")
        parties = []
        for k in ("From", "To"):
            h = value[k]
            if len(h.addresses) != 1 or any(g.display_name for g in h.groups):
                raise QQTrialStopped("alias_evidence_invalid")
            parties.append(h.addresses[0].addr_spec)
        header = str(value["Message-ID"])
        raw_marker = next(v for k, v in value.raw_items()
                          if k.lower() == "x-tradeos-idempotency-v1")
        if (
            tuple(parties) != (sender, recipient)
            or _unfold_token(raw_marker) != idempotency_header
            or not re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", header)
        ):
            raise QQTrialStopped("alias_evidence_invalid")
        if header != canonical_header:
            found.append(
                SentAliasEvidence(
                    source,
                    header,
                    canonical_header,
                    sender,
                    recipient,
                    hashlib.sha256(raw).hexdigest(),
                    raw,
                )
            )
    if len(found) != 1:
        raise QQTrialStopped("alias_missing_or_ambiguous")
    return found[0]
