"""reply_qualification 只读端口：消息内容加载与输入护栏（永不落库/事件/日志）。

客户正文/主题属于原始资料层（docs/architecture/05-data-plane.md 第一层），
只能存在于进程内存与 artifact store 原文中；durable workflow context、
outbox、审计、日志一律不得包含（artifact_store/AGENTS.md 硬边界 4）。
classify 步骤需要原文时经本端口按 message_id 只读加载，找不到返回 None，
调用方必须 fail-closed。生产实现（读 raw_artifact_ref → artifact store）
随入站摄取 connector 接线，本切片以注入 fake 验收。

``InputContentGuard`` 是窄端口：具体护栏实现（agent_runtime/guardrails）
不得反向依赖本模块，故端口签名只收 subject/body 原始串；workflow 组合侧
负责把内容视图字段拆给护栏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
)


@dataclass(frozen=True)
class ReplyMessageContent:
    """只读消息内容视图（仅进程内存，绝不持久化）。

    契约：``body`` 必须非空；``subject`` 允许缺失（真实邮件常无主题），
    进模型前由调用方归一为固定非敏感占位，绝不让模型侧运行时意外失败。
    """

    subject: str | None = field(repr=False)
    body: str = field(repr=False)
    projected: bool = False
    original_subject: str | None = field(default=None, repr=False)
    original_body: str | None = field(default=None, repr=False)
    evidence_segments: tuple[str, ...] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # projected=False仅用于本身未经改写的可信旧reader；已投影不能缺原件。
        if type(self.projected) is not bool or (
            self.projected
            and (
                not isinstance(self.original_body, str)
                or not self.original_body.strip()
                or self.evidence_segments is None
            )
        ):
            raise ValidationError("回复投影缺少可靠原件")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValidationError("回复消息正文无效")
        if self.subject is not None and (
            not isinstance(self.subject, str) or not self.subject.strip()
        ):
            raise ValidationError("回复消息主题无效")


@runtime_checkable
class MessageContentReader(Protocol):
    """只读内容加载端口：按 message_id 取 subject/body。

    原始正文在 artifact store（``Message.raw_artifact_ref``），本端口是
    摄取侧的注入缝；调用方只读、不缓存、不落库。
    """

    async def load(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> ReplyMessageContent | None: ...


@runtime_checkable
class InputContentGuard(Protocol):
    """输入护栏端口：模型永不接触凭证（硬边界 1）。

    在内容送入 classifier/model port **之前**同步检查；违规抛
    ``ValidationError``（固定安全摘要，不回显内容），由 run 转 FAILED
    可观测。签名只收 subject/body 原始串，具体实现位于
    ``agent_runtime/guardrails``（依赖方向：agent_runtime 不得导入
    workflows，故端口不能携带内容视图类型）。
    """

    def check(self, *, subject: str | None, body: str) -> None: ...


@dataclass(frozen=True)
class ReplyActionContext:
    """回复动作只需的关联 ID；禁止携带原文、地址或 artifact 对象键。"""

    message_id: MessageId
    outbound_message_id: OutboundMessageId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId


@runtime_checkable
class ReplyActionPorts(Protocol):
    """非基础回复动作的窄编排出口；每个方法必须自行保持幂等。"""

    async def route_bounce(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...

    async def record_complaint(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...

    async def request_handoff(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...

    async def start_qualification(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...

    async def extract_need_fields(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...

    async def mark_future_restart(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...

    async def create_follow_up(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...

    async def intake_new_contact(
        self, tenant_id: TenantId, context: ReplyActionContext, idempotency_key: str
    ) -> None: ...
