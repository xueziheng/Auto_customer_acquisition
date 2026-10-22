"""当前owner的精确原件读取；正文仅留同调用栈，账本只保存安全ID。"""

import hashlib
from datetime import timedelta
from email.parser import BytesHeaderParser
from email.policy import default

from shared.errors import ValidationError
from shared.schemas.identifiers import new_id
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    ToolCallContext,
    ToolGateway,
)

MANIFEST = ToolManifest(
    tool_id="email.business.reply.inspect",
    version="v1",
    description="核对当前询盘原件与线程关联",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("business_mail:read",),
    checks=("tenant", "permission"),
    input_schema={
        "type": "object",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["provider_ref"],
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
)


class BusinessReplyEvidenceReader:
    def __init__(self, policy, store, fingerprints, ledger):
        self.policy, self.store, self.fingerprints, self.ledger = (
            policy,
            store,
            fingerprints,
            ledger,
        )

    async def read(self, message_id):
        policy, store = self.policy, self.store
        scope = policy.scope
        slot = []

        class Check:
            def __init__(self, name):
                self.name = name

            async def check(self, ctx, state):
                if (
                    ctx.tenant_id != scope.actor.tenant_id
                    or ctx.user_id != scope.actor.user_id
                    or dict(ctx.params) != {"message_id": message_id}
                ):
                    return CheckRejection(
                        self.name, "business_reply:scope", "原件读取范围不匹配"
                    )
                await policy.metadata(message_id)
                return None

        fingerprints = self.fingerprints

        class Handler:
            async def prepare(self, ctx, preflight=None):
                digest, version = fingerprints.fingerprint(
                    (str(ctx.tenant_id).encode(), message_id.encode())
                )
                return PreparedToolCall(
                    digest, version, {"message_id": message_id}, None
                )

            async def execute(self, tenant, prepared):
                meta = await policy.metadata(message_id)
                actual, content = await store.get_bounded(
                    tenant, meta["artifact_id"], maximum_bytes=4 * 1024 * 1024
                )
                if (
                    actual.tenant_id != tenant
                    or actual.artifact_id != meta["artifact_id"]
                    or actual.content_hash != meta["source_sha256"]
                    or hashlib.sha256(content).hexdigest() != meta["source_sha256"]
                ):
                    raise ValidationError("询盘原件完整性校验失败")
                headers = BytesHeaderParser(policy=default).parsebytes(content)
                names = (
                    "From",
                    "To",
                    "Reply-To",
                    "Message-ID",
                    "In-Reply-To",
                    "References",
                    "Auto-Submitted",
                    "List-Id",
                    "Precedence",
                )
                if headers.defects or any(
                    len(headers.get_all(n, [])) > 1 for n in names
                ):
                    raise ValidationError("询盘邮件头存在歧义")
                await policy.metadata(message_id)
                slot.append(
                    {**meta, "headers": {n: str(headers.get(n, "")) for n in names}}
                )
                return {"provider_ref": new_id("bre")}

        registry = ToolRegistry()
        registry.register(MANIFEST, Handler())
        gateway = ToolGateway(
            registry,
            {n: Check(n) for n in MANIFEST.checks},
            self.ledger,
            lease_duration=timedelta(seconds=30),
            lease_owner="business_reply_evidence",
            id_factory=new_id,
        )
        try:
            result = await gateway.invoke(
                ToolCallContext(
                    tenant_id=scope.actor.tenant_id,
                    user_id=scope.actor.user_id,
                    tool_id=MANIFEST.tool_id,
                    params={"message_id": message_id},
                )
            )
            if result.status.value != "succeeded" or len(slot) != 1:
                raise ValidationError("询盘原件读取未完成")
            await policy.metadata(message_id)
            return slot.pop()
        finally:
            slot.clear()
