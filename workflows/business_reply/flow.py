"""先校验来源、再由Gateway发送；真实回信只交给原owner，不创造业务成果。"""

import json
from email.utils import getaddresses

from domains.conversations.business_mail_followup import (
    BusinessMailDraftCommand,
    BusinessMailHandlingCommand,
)
from domains.conversations.business_reply import BODY, eligible_reply, source_evidence
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, UserId
from tool_gateway.handlers.business_reply import MANIFEST
from tool_gateway.pipeline import ToolCallContext


class BusinessReplyDriver:
    def __init__(self, scope, policy, followup, evidence, gateway, handler, heartbeat):
        self.scope, self.policy, self.followup, self.evidence = (
            scope,
            policy,
            followup,
            evidence,
        )
        self.gateway, self.handler, self.heartbeat = gateway, handler, heartbeat

    async def _note(self, key, status, note):
        current = await self.followup.get(self.scope.actor, self.scope.message_id)
        if any(h.request_key == key for h in current.history):
            return current
        return await self.followup.handle(
            self.scope.actor,
            self.scope.message_id,
            BusinessMailHandlingCommand(
                expected_revision=current.revision, status=status, note=note
            ),
            idempotency_key=key,
        )

    async def once(self, *, reconcile=False):
        await self.heartbeat()
        await self.policy.authorize()
        scope = self.scope
        current = await self.followup.get(scope.actor, scope.message_id)
        received = set()
        complete = current.status == "completed"
        for event in current.history:
            if not event.request_key.startswith("business-reply-received:"):
                continue
            # 兼容第一版单封历史；补充回信各自留痕，不能覆盖原件或再次发信。
            try:
                prior = json.loads("{" + event.note.partition("{")[2])
                received.add(prior["source_id"])
                complete = complete or not prior["missing_fields"]
            except (ValueError, KeyError, TypeError) as error:
                raise ValidationError("回信历史需人工核对，禁止盲目继续") from error
        if complete:
            return {
                "status": "owner_review",
                "message_id": scope.message_id,
                "owner_id": scope.actor.employee_id,
            }
        record = await self.policy.send_record()
        if record and record["status"] != "succeeded" and not reconcile:
            return {
                **record,
                "send_status": record["status"],
                "status": "needs_reconciliation",
            }
        if record is None or record["status"] != "succeeded":
            source = await self.evidence.read(scope.message_id)
            headers = source["headers"]
            if (
                headers["Message-ID"] != scope.original_message_id
                or headers["Auto-Submitted"].lower() not in {"", "no"}
                or headers["List-Id"]
                or headers["Precedence"].lower() in {"bulk", "list", "junk"}
                or (
                    headers["Reply-To"]
                    and getaddresses([headers["Reply-To"]])
                    != getaddresses([headers["From"]])
                )
            ):
                raise ValidationError("原始询盘不满足单封追问条件")
            # 新回信已经到达时不再发送过时的追问，留给owner处理。
            if await self.policy.candidates():
                raise ValidationError("已有同发件人新来信，请先核对再决定追问")
            draft_key = "business-reply-draft:" + scope.message_id
            if not any(h.request_key == draft_key for h in current.history):
                current = await self.followup.save_draft(
                    scope.actor,
                    scope.message_id,
                    BusinessMailDraftCommand(
                        expected_revision=current.revision,
                        subject="Re: [TEST-04] YJ-168 purchase inquiry",
                        body_text=BODY,
                    ),
                    idempotency_key=draft_key,
                )
            if (
                current.draft is None
                or current.draft.body_text != BODY.strip()
                or current.status not in {"pending", "in_progress"}
            ):
                raise ValidationError("草稿或处理状态已由他人改变")
            await self.heartbeat()
            result = await self.gateway.invoke(
                ToolCallContext(
                    tenant_id=scope.actor.tenant_id,
                    user_id=UserId(scope.actor.employee_id),
                    tool_id=MANIFEST.tool_id,
                    params={"message_id": scope.message_id},
                    idempotency_key=IdempotencyKey(scope.key),
                    approval_ref=scope.message_id,
                )
            )
            record = await self.policy.send_record()
            if (
                result.status.value not in {"succeeded", "duplicate"}
                or not record
                or record["status"] != "succeeded"
            ):
                return {
                    "status": "needs_reconciliation",
                    "tool_call_id": result.tool_call_id,
                    "error_category": result.error_category.value
                    if result.error_category
                    else None,
                    "rejected_stage": result.rejected.stage
                    if result.rejected
                    else None,
                }
        await self._note(
            "business-reply-sent:" + scope.message_id,
            "waiting_customer",
            "系统按用户授权自动追问用途与数量；SMTP已接受，等待真实回信。发送账本："
            + record["tool_call_id"]
            + "；内部测试，不计需求或商机。",
        )
        outgoing = "<" + self.handler.envelope()[0].deterministic_message_id + ">"
        for message_id in await self.policy.candidates():
            if message_id in received:
                continue
            await self.heartbeat()
            reply = await self.evidence.read(message_id)
            if not eligible_reply(scope, reply["headers"], outgoing):
                continue
            evidence = source_evidence(
                message_id=message_id,
                artifact_id=reply["artifact_id"],
                source_sha256=reply["source_sha256"],
                body=reply["body"],
            )
            # 处理说明上限明确；详细原文仍在原邮件及不可变原件，不能截断证据伪造完整性。
            summary = {k: v for k, v in evidence.items() if k != "original_text"}
            note = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
            if len(note) > 1800:
                summary.pop("fields")
                summary["review_required"] = "字段较长，负责人打开原件逐项核对"
                note = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
            await self._note(
                "business-reply-received:" + scope.message_id + ":" + message_id,
                "in_progress",
                "系统已关联真实回信并交当前负责人核对；未冒充人工完成。" + note,
            )
            return {
                "status": "waiting_customer_details"
                if evidence["missing_fields"]
                else "owner_review",
                "message_id": scope.message_id,
                "reply_message_id": message_id,
                "owner_id": scope.actor.employee_id,
                "evidence": evidence,
            }
        return {
            **record,
            "send_status": record["status"],
            "status": "waiting_customer_details" if received else "waiting_customer",
            "message_id": scope.message_id,
        }
