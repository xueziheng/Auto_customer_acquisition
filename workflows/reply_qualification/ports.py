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

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shared.errors import ValidationError
from shared.schemas.identifiers import MessageId, TenantId


@dataclass(frozen=True)
class ReplyMessageContent:
    """只读消息内容视图（仅进程内存，绝不持久化）。

    契约：``body`` 必须非空；``subject`` 允许缺失（真实邮件常无主题），
    进模型前由调用方归一为固定非敏感占位，绝不让模型侧运行时意外失败。
    """

    subject: str | None
    body: str

    def __post_init__(self) -> None:
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
