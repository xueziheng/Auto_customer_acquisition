"""单封已收询盘的受控追问；测试来源永不晋升真实需求。"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from domains.conversations.business_mail import BusinessMailActor, MailDTO
from shared.errors import ValidationError

BODY = (
    "Thank you for your internal workflow test inquiry about the YJ-168 solar pedestal fan.\n\n"
    "To continue this test, could you confirm the intended application and approximate quantity? "
    "Please label any simulated details as test data.\n\n"
    "Our team will review the product specifications and minimum order quantity separately. "
    "This message does not confirm specifications, MOQ, pricing, availability or delivery dates.\n\n"
    "This remains an internal workflow test, not a purchase order.\n"
)


class BusinessReplyScope(MailDTO):
    """当前接力只开放逐条显式批准的内部测试，绝不扫描历史邮件。"""

    enabled: Literal[True]
    actor: BusinessMailActor
    message_id: str = Field(pattern=r"^bmi_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sender: str
    recipient: str
    binding_operation_id: str
    identity_id: str
    identity_version: int = Field(strict=True, ge=0)
    original_message_id: str
    approved_at: datetime
    expires_at: datetime
    max_sends: Literal[1]
    purpose: Literal["internal_workflow_test"]
    approval_source: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_scope(self):
        if (
            self.approved_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or not timedelta(0)
            < self.expires_at - self.approved_at
            <= timedelta(hours=24)
        ):
            raise ValueError("接力授权期限无效")
        if not re.fullmatch(
            r"<[^<>\s@]{1,200}@[^<>\s@]{1,200}>", self.original_message_id
        ):
            raise ValueError("原信关联头无效")
        for address in (self.sender, self.recipient):
            if not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                address,
            ):
                raise ValueError("邮件地址无效")
        if self.sender == self.recipient:
            raise ValueError("不允许自回复循环")
        return self

    @property
    def key(self) -> str:
        # 与重新授权时间无关；同一原信永远只对应一次发送。
        return "business-reply:" + self.message_id

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def validate_source(
    scope: BusinessReplyScope,
    *,
    sender: str,
    subject: str,
    body: str,
    header: str | None,
    source_sha256: str,
    source_kind: str,
    category: str,
    status: str,
) -> None:
    """选中原件的精确绑定；模型business标签本身不足以允许自动发送。"""
    from email.utils import getaddresses

    addresses = getaddresses([sender])
    if (
        len(addresses) != 1
        or addresses[0][1].casefold() != scope.recipient.casefold()
        or source_sha256 != scope.source_sha256
        or source_kind != "full_mime"
        or category != "business"
        or status != "classified"
        or header != scope.original_message_id
        or subject != "[TEST-04] YJ-168 purchase inquiry"
        or " ".join(body.split())
        != (
            "This is an internal workflow test, not a real purchase order. "
            "I am interested in the YJ-168 solar pedestal fan. Please send the specifications and minimum order quantity."
        )
    ):
        raise ValidationError("来源与批准的内部测试询盘不一致")


def source_evidence(
    *, message_id: str, artifact_id: str, source_sha256: str, body: str
) -> dict:
    """保留原话与来源，不把模拟数量或推断转成已验证需求。"""
    fields = {}
    for line in body.splitlines():
        if line.lstrip().startswith(">") or re.match(
            r"^(On .+wrote:|[- ]*Original Message|发件人[:：]|来自阿里邮箱)",
            line.lstrip(),
        ):
            break
        # 手机邮箱会折叠换行；只按显式字段标签切分，原话保留精确子串。
        labels = list(
            re.finditer(
                r"(?<!\S)(Product|Application|Quantity):[ \t]*", line, re.IGNORECASE
            )
        )
        for index, match in enumerate(labels):
            end = labels[index + 1].start() if index + 1 < len(labels) else len(line)
            value = line[match.end() : end].strip()
            key = match.group(1).lower()
            if not 1 <= len(value) <= 300 or key in fields:
                continue
            fields[key] = {
                "value": value,
                "evidence_quote": line[match.start() : end].strip(),
                "source_type": "customer_test_statement",
                "source_id": message_id,
                "artifact_id": artifact_id,
                "source_sha256": source_sha256,
                "extracted_by": "business_reply_literal_v2",
                "confirmed_by": None,
                "confirmed_at": None,
            }
    return {
        "purpose": "internal_workflow_test",
        "fields": fields,
        "source_id": message_id,
        "artifact_id": artifact_id,
        "source_sha256": source_sha256,
        "original_text": body,
        "validated_need": False,
        "opportunity": False,
        "missing_fields": [f for f in ("application", "quantity") if f not in fields],
    }


def eligible_reply(
    scope: BusinessReplyScope, headers: dict, outgoing_message_id: str
) -> bool:
    """只接受同发件人、当前邮箱及真实References关联；机器人/群发不推进。"""
    from email.utils import getaddresses

    senders = getaddresses([headers.get("From", "")])
    recipients = getaddresses([headers.get("To", "")])
    refs = set(
        re.findall(
            r"<[^<>\s]+>",
            headers.get("References", "") + " " + headers.get("In-Reply-To", ""),
        )
    )
    return (
        len(senders) == 1
        and senders[0][1].casefold() == scope.recipient.casefold()
        and scope.sender.casefold() in {a.casefold() for _, a in recipients}
        and headers.get("Message-ID")
        not in {scope.original_message_id, outgoing_message_id, ""}
        and bool(refs & {scope.original_message_id, outgoing_message_id})
        and headers.get("Auto-Submitted", "").lower() in {"", "no"}
        and not headers.get("List-Id")
        and headers.get("Precedence", "").lower() not in {"bulk", "list", "junk"}
    )
