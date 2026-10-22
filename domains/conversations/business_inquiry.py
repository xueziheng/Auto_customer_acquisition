"""普通询盘的模型提案契约；逐字来源与受控下一问，永不代替人工确认。"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from email.utils import getaddresses
from typing import Literal

from pydantic import Field, model_validator

from domains.conversations.business_mail import BusinessMailActor, MailDTO
from domains.conversations.business_reply import BusinessReplyScope
from shared.errors import ValidationError

FieldName = Literal["product", "application", "quantity"]
FIELDS = ("product", "application", "quantity")
QUESTIONS = {
    "product": "Which product or model are you interested in?",
    "application": "What is the intended application?",
    "quantity": "What approximate quantity and unit do you require?",
}


class InquiryAutomationScope(MailDTO):
    """显式邮箱授权及时间边界；验收收件范围来自配置而非业务代码。"""

    enabled: Literal[True]
    actor: BusinessMailActor
    sender: str
    mailbox_id: str
    binding_operation_id: str
    identity_id: str
    identity_version: int = Field(strict=True, ge=0)
    approved_at: datetime
    expires_at: datetime
    allowed_recipients: tuple[str, ...] = Field(min_length=1, max_length=10)
    recovery_message_ids: tuple[str, ...] = Field(max_length=10)
    purpose: Literal["internal_workflow_test"]
    approval_source: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def bounded(self):
        if (
            self.approved_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or not timedelta(0)
            < self.expires_at - self.approved_at
            <= timedelta(hours=24)
            or any(
                not re.fullmatch(r"bmi_[0-7][0-9A-HJKMNP-TV-Z]{25}", x)
                for x in self.recovery_message_ids
            )
        ):
            raise ValueError("自动跟进范围无效")
        for address in (self.sender, *self.allowed_recipients):
            if not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                address,
            ):
                raise ValueError("邮箱地址无效")
        if self.sender.casefold() in {x.casefold() for x in self.allowed_recipients}:
            raise ValueError("不允许自动自回复")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class InquiryField(MailDTO):
    name: FieldName
    value: str = Field(min_length=1, max_length=120)
    evidence_quote: str = Field(min_length=1, max_length=240)


class InquiryAnalysis(MailDTO):
    intent: Literal["buyer_inquiry", "customer_details", "stop", "other", "review"]
    reason: str = Field(min_length=1, max_length=180)
    intent_quote: str = Field(max_length=240)
    fields: tuple[InquiryField, ...] = Field(max_length=3)
    next_fields: tuple[FieldName, ...] = Field(max_length=2)


def inquiry_analysis_status(
    source: dict, *, followup: bool
) -> Literal["ready", "waiting_classification", "reply_review_required"]:
    """分类未完成不得消耗模型claim；同线程的模型待确认类仅允许提取供人工核对。"""
    status = source.get("classification_status")
    if status in {"pending", "classifying"}:
        return "waiting_classification"
    if status == "classified" and (
        source.get("category") == "business"
        or (
            followup
            and source.get("category") == "review"
            and source.get("classification_origin") == "model"
        )
    ):
        return "ready"
    return "reply_review_required"


def customer_text(body: str) -> str:
    """只给本次客户正文，引用历史与签名不能补成客户当前声明。"""
    lines = []
    for line in body.splitlines():
        if line.lstrip().startswith(">") or re.match(
            r"^(On .+wrote:|[- ]*Original(?: Message)?|[- ]*原始邮件|发件人[:：]|From:|来自阿里邮箱|--\s*$)",
            line.lstrip(),
            re.IGNORECASE,
        ):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def validate_analysis(raw: str, body: str, *, followup: bool) -> InquiryAnalysis:
    """模型建议必须逐字有据；缺项与最多两问由确定性代码再次核验。"""
    result = InquiryAnalysis.model_validate_json(raw)
    source = customer_text(body)
    names = [f.name for f in result.fields]
    if (
        len(names) != len(set(names))
        or len(result.next_fields) != len(set(result.next_fields))
        or any(
            f.value not in f.evidence_quote or f.evidence_quote not in source
            for f in result.fields
        )
        or any(f in names for f in result.next_fields)
        or (
            result.intent in {"buyer_inquiry", "customer_details"}
            and (not result.intent_quote or result.intent_quote not in source)
        )
        or (not followup and result.intent == "customer_details")
    ):
        raise ValidationError("模型询盘提案缺少精确来源")
    if (
        re.search(
            r"(?i)unsubscribe|do not (?:email|contact)|stop (?:email|contact)|退订|不要再联系",
            source,
        )
        and result.intent != "stop"
    ):
        raise ValidationError("停止联系要求不能进入自动追问")
    missing = {f for f in FIELDS if f not in names}
    if result.intent == "buyer_inquiry" and missing and not result.next_fields:
        raise ValidationError("缺项未进入下一问")
    return result


def inquiry_headers(headers: dict, sender: str, recipient: str) -> bool:
    """只接受完整、单收件人、非自动/列表、无异址Reply-To的直接新询盘。"""
    addresses = lambda name: [
        a.casefold() for _, a in getaddresses([headers.get(name, "")])
    ]
    return (
        addresses("From") == [recipient.casefold()]
        and addresses("To") == [sender.casefold()]
        and (
            not headers.get("Reply-To")
            or addresses("Reply-To") == [recipient.casefold()]
        )
        and not headers.get("In-Reply-To")
        and not headers.get("References")
        and headers.get("Auto-Submitted", "").lower() in {"", "no"}
        and not headers.get("List-Id")
        and headers.get("Precedence", "").lower() not in {"bulk", "list", "junk"}
        and bool(
            re.fullmatch(
                r"<[^<>\s@]{1,200}@[^<>\s@]{1,200}>", headers.get("Message-ID", "")
            )
        )
    )


def reply_text(analysis: InquiryAnalysis) -> str:
    """模型选择问什么；英文模板不允许插入未经批准的商业承诺。"""
    if analysis.intent != "buyer_inquiry" or not analysis.next_fields:
        raise ValidationError("当前提案不允许自动追问")
    return (
        "Thank you for your inquiry.\n\n"
        + "\n".join(QUESTIONS[f] for f in analysis.next_fields)
        + "\n\nOur team will review your request for product information. "
        "Specifications, minimum order quantity, pricing and delivery dates remain to be confirmed.\n\n"
        "This is an internal workflow test, not a purchase order. Please label simulated details as test data.\n"
    )


class InquiryReplyScope(BusinessReplyScope):
    """与旧TEST-04序列化隔离；同原信仍复用旧Gateway稳定业务键。"""

    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: int = Field(strict=True, ge=0)
    model_run_id: str
    subject: str = Field(min_length=1, max_length=500)
    analysis: InquiryAnalysis

    @property
    def body_text(self) -> str:
        return reply_text(self.analysis)


def evidence_note(analysis: InquiryAnalysis, source: dict, run_id: str) -> str:
    """压缩但完整保留来源；确认人为空，模型提取不能变成人工确认。"""
    value = {
        "source_id": source["message_id"],
        "artifact_id": source["artifact_id"],
        "source_sha256": source["source_sha256"],
        "extracted_by": run_id,
        "confirmed_by": None,
        "confirmed_at": None,
        "source_type": "customer_test_statement",
        "validated_need": False,
        "opportunity": False,
        "analysis": analysis.model_dump(),
    }
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(result) > 1950:
        raise ValidationError("来源提案超过处理记录容量，需人工核对")
    return result
