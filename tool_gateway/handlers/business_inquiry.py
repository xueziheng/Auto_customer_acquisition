"""模型受控提案的邮件材料；复用既有HIGH六阶段和未知结果对账。"""

from connectors.qq_mail.business_reply import QQReplyEnvelope
from tool_gateway.handlers.business_reply import BusinessReplyHandler


class InquiryReplyHandler(BusinessReplyHandler):
    def envelope(self):
        s = self.scope
        digest, version = self.fingerprints.fingerprint(
            tuple(
                v.encode()
                for v in (
                    "business-inquiry-v1",
                    s.digest,
                    s.key,
                    s.subject,
                    s.body_text,
                )
            )
        )
        return (
            QQReplyEnvelope(
                s.sender,
                s.recipient,
                s.subject,
                s.body_text,
                f"businessreply.{digest}@messages.tradeos.invalid",
                f"businessreply.{digest}",
                s.original_message_id,
            ),
            digest,
            version,
        )
