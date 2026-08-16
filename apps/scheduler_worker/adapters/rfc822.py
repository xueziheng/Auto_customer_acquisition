"""确定性 RFC822 邮件解析：抽取 subject 与首个非 attachment text/plain 正文。

- 纯标准库（``email``），无业务规则、无 DB、无凭证、无外部依赖。
- 调用方负责大小门限（本模块不做 bytes 截断，只做解析与视图构造）。
- 解析失败 / 无 text/plain 正文 / 正文为空 → 固定摘要 ``ValidationError``，
  不回显原文或地址（正文可能含凭证，绝不进异常消息）。
- HTML-only 回复 fail-closed（不剥离 HTML）：成本记录——此类回复（少见）
  本片不分类，run FAILED 可观测，留待人工；不引入 HTML 解析依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser

from shared.errors import ValidationError

_NO_PLAIN_TEXT = "回复消息正文不可解析"
_EMPTY_BODY = "回复消息正文无效"


@dataclass(frozen=True)
class Rfc822View:
    """解析出的 subject/body（仅内存；body 保证非空）。"""

    subject: str | None
    body: str


def parse_email_rfc822(data: bytes) -> Rfc822View:
    """确定性解析 RFC822 bytes → (subject, body)。

    - subject：取 ``Subject`` 头（policy=default 按 RFC 2047 解码）；缺失/空 → None
    - body：walk 取首个 ``Content-Disposition != attachment`` 且
      ``Content-Type`` 为 text/plain 的部分；charset/transfer-encoding 由
      ``policy=default`` 确定性解码（quoted-printable/base64/8bit）
    - 无 text/plain（含 HTML-only）→ ValidationError
    - 正文解码后为空/纯空白 → ValidationError
    """
    if not isinstance(data, bytes):
        raise ValidationError("回复消息原文无效")
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
    except (ValueError, TypeError):
        raise ValidationError(_NO_PLAIN_TEXT) from None
    raw_subject = message.get("Subject")
    subject = str(raw_subject).strip() if raw_subject is not None else None
    if subject == "":
        subject = None
    for part in message.walk():
        disposition = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in disposition:
            continue
        if part.get_content_type() != "text/plain":
            continue
        try:
            body = part.get_content()
        except (LookupError, ValueError, TypeError):
            # 未知 charset / 畸形编码：确定性失败，不回显内容
            raise ValidationError(_NO_PLAIN_TEXT) from None
        if not isinstance(body, str):
            continue
        if not body.strip():
            continue
        return Rfc822View(subject=subject, body=body)
    # 无 text/plain 部分（含 HTML-only）或全部为空
    raise ValidationError(_NO_PLAIN_TEXT if _has_no_plain(message) else _EMPTY_BODY)


def _has_no_plain(message: object) -> bool:
    """walk 后是否完全不存在 text/plain 部分（决定错误摘要）。"""
    for part in message.walk():  # type: ignore[attr-defined]
        if part.get_content_type() == "text/plain":
            return False
    return True
