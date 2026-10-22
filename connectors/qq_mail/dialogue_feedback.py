"""复用严格DSN/ARF结构解析，再绑定原发送双方；主题和发件人不决定反馈类型。"""
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from hashlib import sha256

from connectors.gmail.arf import parse_abuse_report
from connectors.gmail.feedback import message_occurred_at, parse_delivery_status
from shared.dialogue_feedback import DialogueDeliveryReport
from shared.errors import ValidationError
from shared.schemas.business_reply import (
    validate_reply_address,
    validate_reply_message_id,
)


def parse_dialogue_report(raw: bytes) -> DialogueDeliveryReport | None:
    """只支持单收件人报告；缺原始Message-ID/幂等头或歧义一律人工核对。"""
    if not 0 < len(raw) <= 4 * 1024 * 1024:
        return None
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.get_content_type() != 'multipart/report':
        return None
    ref = sha256(raw).hexdigest()
    items = parse_abuse_report(raw, provider_ref_digest=ref, occurred_at=message_occurred_at(raw)) if message.get_param('report-type') == 'feedback-report' else parse_delivery_status(raw, ref)
    if len(items) != 1 or items[0].kind.value not in {'hard_bounce', 'soft_bounce', 'complaint'}:
        return None
    item = items[0]
    correlation = item.correlation
    if correlation is None or correlation.deterministic_message_id is None or correlation.idempotency_header is None:
        return None
    parts = [part for part in message.walk() if part.get_content_type() == 'message/rfc822']
    if len(parts) != 1:
        return None
    payload = parts[0].get_payload()
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], Message):
        return None
    original = payload[0]
    if len(original.get_all('From', [])) != 1 or len(original.get_all('To', [])) != 1 or original.get_all('Cc') or original.get_all('Bcc'):
        return None
    senders, recipients = getaddresses(original.get_all('From', [])), getaddresses(original.get_all('To', []))
    if len(senders) != 1 or len(recipients) != 1:
        return None
    sender, recipient = senders[0][1], recipients[0][1]
    header = f'<{correlation.deterministic_message_id}>'
    try:
        validate_reply_address(sender); validate_reply_address(recipient); validate_reply_message_id(header)
        if item.kind.value != 'complaint':
            reports = [part for part in message.walk() if part.get_content_type() == 'message/delivery-status']
            blocks = reports[0].get_payload() if len(reports) == 1 else None
            finals = [str(block.get('Final-Recipient')) for block in blocks if isinstance(block, Message) and block.get('Final-Recipient')] if isinstance(blocks, list) else []
            if len(finals) != 1 or finals[0].split(';', 1)[-1].strip() != recipient:
                return None
        else:
            feedback = [part for part in message.walk() if part.get_content_type() == 'message/feedback-report']
            reported: list[str] = []
            for part in feedback:
                report_payload = part.get_payload()
                blocks = [part]
                if isinstance(report_payload, list):
                    blocks.extend(block for block in report_payload if isinstance(block, Message))
                for block in blocks:
                    if not isinstance(block, Message):
                        continue
                    reported.extend(str(value) for value in block.get_all('Original-Rcpt-To', []))
            if reported and (len(reported) != 1 or reported[0].split(';', 1)[-1].strip() != recipient):
                return None
        return DialogueDeliveryReport.model_validate({'kind': item.kind.value, 'original_message_id': header,
            'original_idempotency_header': correlation.idempotency_header, 'original_sender': sender,
            'original_recipient': recipient, 'occurred_at': item.occurred_at})
    except (ValueError, ValidationError):
        return None
