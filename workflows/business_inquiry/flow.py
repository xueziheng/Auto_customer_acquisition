"""模型建议、Gateway发送、原线程回信和人工接管的持久推进。"""

import json
from email.utils import getaddresses

from domains.conversations.business_inquiry import (
    InquiryAnalysis,
    InquiryReplyScope,
    evidence_note,
    inquiry_analysis_status,
    inquiry_headers,
)
from domains.conversations.business_mail_followup import BusinessMailDraftCommand
from domains.conversations.business_reply import eligible_reply
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, UserId, new_id
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError
from tool_gateway.handlers.business_reply import MANIFEST
from tool_gateway.pipeline import ToolCallContext


class BusinessInquiryDriver:
    def __init__(
        self,
        records,
        read_source,
        plan_factory,
        send_factory,
        heartbeat,
        *,
        model,
        version,
    ):
        self.records, self.read_source = records, read_source
        self.plan_factory, self.send_factory, self.heartbeat = (
            plan_factory,
            send_factory,
            heartbeat,
        )
        self.model, self.version = model, version

    async def analyze(self, original, source, *, followup):
        if inquiry_analysis_status(source, followup=followup) != "ready":
            return None
        key = original + ":" + source["message_id"]
        done = await self.records.event(original, "inquiry-analysis:" + key)
        if done:
            claim = await self.records.event(original, "inquiry-claim:" + key)
            if (
                not claim
                or json.loads(claim.note)["source_revision"] != source["revision"]
                or json.loads(done.note)["source_sha256"] != source["source_sha256"]
            ):
                raise ValidationError("提案来源已变化，需要人工核对")
            return InquiryAnalysis.model_validate(
                json.loads(done.note)["analysis"]
            ), json.loads(done.note)["extracted_by"]
        if await self.records.event(original, "inquiry-claim:" + key):
            return None  # 结果未知或失败：保留原run，不能换键再次消费模型。
        identity = InvocationIdentity(
            **self.records.scope.actor.model_dump(),
            run_id=new_id("run"),
            sequence=0,
            capability="business_mail_followup",
            configuration_version=self.version,
        )
        claim = {
            "identity": identity.model_dump(),
            "source_id": source["message_id"],
            "followup": followup,
            "source_sha256": source["source_sha256"],
            "source_revision": source["revision"],
            "policy_digest": self.records.scope.digest,
            "model": self.model,
        }
        await self.records.append(
            original,
            "inquiry-claim:" + key,
            "in_progress",
            json.dumps(claim, ensure_ascii=False, separators=(",", ":")),
        )
        try:
            planner = self.plan_factory(original, source, identity)
            analysis = await planner.analyze(identity, source, followup=followup)
            note = evidence_note(analysis, source, str(identity.run_id))
        except (ModelGenerationError, ValidationError, ValueError) as error:
            await self.records.append(
                original,
                "inquiry-failed:" + key,
                "in_progress",
                "模型跟进未能交付可用提案，等待人工核对；不自动重试。原因："
                + (
                    error.code
                    if isinstance(error, ModelGenerationError)
                    else "invalid_source_or_output"
                ),
            )
            return None
        await self.heartbeat()
        await self.records.append(
            original, "inquiry-analysis:" + key, "in_progress", note
        )
        return analysis, str(identity.run_id)

    async def once(self, original):
        await self.heartbeat()
        config = self.records.scope
        current = await self.records.view(original)
        if current.status == "completed":
            return "human_completed"
        if any(e.request_key == "inquiry-skip:" + original for e in current.history):
            return "manual_review"
        # 不接管其他流程或覆盖已有人工作业的记录。
        if current.history and not any(
            e.request_key.startswith("inquiry-") for e in current.history
        ):
            return "existing_owner_work"
        saved = await self.records.event(original, "inquiry-scope:" + original)
        if saved:
            scope = InquiryReplyScope.model_validate_json(saved.note)
        else:
            source = await self.read_source(original, original)
            senders = getaddresses([source["headers"]["From"]])
            recipient = senders[0][1] if len(senders) == 1 else ""
            if not inquiry_headers(source["headers"], config.sender, recipient):
                await self.records.append(
                    original,
                    "inquiry-skip:" + original,
                    "in_progress",
                    "系统未将此邮件作为新询盘自动回复；可能是既有线程回复、自动邮件或邮件头需核对，保留原件。",
                )
                return "manual_review"
            readiness = inquiry_analysis_status(source, followup=False)
            if readiness != "ready":
                return readiness
            result = await self.analyze(original, source, followup=False)
            if result is None:
                return "model_review_required"
            analysis, run_id = result
            if analysis.intent != "buyer_inquiry" or not analysis.next_fields:
                await self.records.append(
                    original,
                    "inquiry-skip:" + original,
                    "in_progress",
                    "模型提案不需要自动追问，已交当前负责人核对；未自动确认采购或商机。",
                )
                return "owner_review"
            scope = InquiryReplyScope(
                enabled=True,
                actor=config.actor,
                message_id=original,
                source_sha256=source["source_sha256"],
                sender=config.sender,
                recipient=recipient,
                binding_operation_id=config.binding_operation_id,
                identity_id=config.identity_id,
                identity_version=config.identity_version,
                original_message_id=source["headers"]["Message-ID"],
                approved_at=config.approved_at,
                expires_at=config.expires_at,
                max_sends=1,
                purpose=config.purpose,
                approval_source=config.approval_source,
                policy_digest=config.digest,
                source_revision=source["revision"],
                model_run_id=run_id,
                subject="Re: " + (source["subject"][:495] or "Your inquiry"),
                analysis=analysis,
            )
            await self.records.append(
                original,
                "inquiry-scope:" + original,
                "in_progress",
                scope.model_dump_json(),
            )
        policy, handler, gateway = self.send_factory(scope)
        await policy.authorize()
        record = await policy.send_record()
        if record and record["status"] != "succeeded":
            return "send_reconciliation_required"
        if not record:
            current = await self.records.view(original)
            # 先查看线程是否已有客户补充，避免给已经回答的客户重复追问。
            for candidate in await policy.candidates():
                reply = await self.read_source(original, candidate)
                if eligible_reply(
                    scope,
                    reply["headers"],
                    "<" + handler.envelope()[0].deterministic_message_id + ">",
                ):
                    await self.records.append(
                        original,
                        "inquiry-skip:" + original,
                        "in_progress",
                        "客户已在原线程补充来信，取消尚未发送的追问，转负责人核对。",
                    )
                    return "owner_review"
            key = "inquiry-draft:" + original
            if not any(e.request_key == key for e in current.history):
                current = await self.records.followup.save_draft(
                    config.actor,
                    original,
                    BusinessMailDraftCommand(
                        expected_revision=current.revision,
                        subject=scope.subject,
                        body_text=scope.body_text,
                    ),
                    idempotency_key=key,
                )
            if (
                current.draft is None
                or current.draft.body_text != scope.body_text.strip()
                or current.draft.subject != scope.subject
                or current.status != "in_progress"
            ):
                raise ValidationError("询盘草稿或人工状态已改变")
            await self.heartbeat()
            await gateway.invoke(
                ToolCallContext(
                    tenant_id=config.actor.tenant_id,
                    user_id=UserId(config.actor.employee_id),
                    tool_id=MANIFEST.tool_id,
                    params={"message_id": original},
                    idempotency_key=IdempotencyKey(scope.key),
                    approval_ref=original,
                )
            )
            record = await policy.send_record()
            if not record or record["status"] != "succeeded":
                return "send_reconciliation_required"
        sent_key = "inquiry-sent:" + original
        if not await self.records.event(original, sent_key):
            await self.records.append(
                original,
                sent_key,
                "waiting_customer",
                "系统按模型提案自动发送必要追问，SMTP已接受，等待客户回信。发送账本："
                + record["tool_call_id"]
                + "。内部测试，不计入真实需求或商机。",
            )
        outgoing = "<" + handler.envelope()[0].deterministic_message_id + ">"
        deferred = None
        for candidate in reversed(await policy.candidates()):
            key = original + ":" + candidate
            if await self.records.event(original, "inquiry-received:" + key):
                continue
            reply = await self.read_source(original, candidate)
            if not eligible_reply(scope, reply["headers"], outgoing):
                continue
            readiness = inquiry_analysis_status(reply, followup=True)
            if readiness != "ready":
                deferred = readiness
                continue
            result = await self.analyze(original, reply, followup=True)
            if result is None:
                return "model_review_required"
            analysis, _ = result
            await self.records.append(
                original,
                "inquiry-received:" + key,
                "in_progress",
                "系统已关联同线程客户回信，提取结果及原文来源见上方记录，等待当前负责人确认；未自动完成。",
            )
            return "owner_review"
        current = await self.records.view(original)
        return (
            "owner_review"
            if any(
                e.request_key.startswith("inquiry-received:") for e in current.history
            )
            else deferred or "waiting_customer"
        )
