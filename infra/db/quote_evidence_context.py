"""来源授权的短事务metadata投影，不持业务锁、不读取原文。"""

import json
import re
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import ConversationRow, EmployeeRow, MessageRow, ValidatedNeedRow
from shared.schemas.evidence_read import (
    NeedQuantitySourceFact,
    QuoteEvidenceError,
    QuoteMessageReferenceFact,
)
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId, ValidatedNeedId
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_identity


def _identity(tenant_id: TenantId, resource: str, prefix: str | None) -> None:
    try:
        if (
            type(tenant_id) is not str
            or re.fullmatch(r"tn_[0-7][0-9A-HJKMNP-TV-Z]{25}", tenant_id) is None
        ):
            raise ValueError
        if prefix is None:
            fact_identity(resource)
        elif (
            type(resource) is not str
            or re.fullmatch(rf"{prefix}_[0-7][0-9A-HJKMNP-TV-Z]{{25}}", resource)
            is None
        ):
            raise ValueError
    except ValueError:
        raise QuoteEvidenceError("invalid_input") from None


class SqlAlchemyQuoteEvidenceContextReader:
    """只读真实字段，角色及用途判断由域和workflow负责。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        statement_timeout_ms: int,
    ) -> None:
        self._factory, self._timeout = session_factory, statement_timeout_ms
        if type(statement_timeout_ms) is not int or statement_timeout_ms <= 0:
            raise QuoteEvidenceError("invalid_input")

    async def _one(self, statement: Select[Any]) -> RowMapping | None:
        """事务在投影返回前结束，超时必填，不加任何业务锁。"""
        try:
            async with self._factory() as session:
                await session.execute(
                    text("SELECT set_config('statement_timeout', :value, true)"),
                    {"value": f"{self._timeout}ms"},
                )
                result = (await session.execute(statement)).mappings().one_or_none()
            return result
        except Exception:  # noqa: BLE001 - SQL/重复行固定错误，无连接或原始字段
            raise QuoteEvidenceError("source_unavailable") from None

    async def read_actor(
        self, tenant_id: TenantId, actor_id: EmployeeId
    ) -> QuoteEmployeeFact | None:
        """原样投影当前员工，不在SQL里判权。"""
        _identity(tenant_id, actor_id, None)
        row = await self._one(
            select(
                *(getattr(EmployeeRow, name) for name in QuoteEmployeeFact.model_fields)
            ).where(
                EmployeeRow.tenant_id == tenant_id, EmployeeRow.employee_id == actor_id
            )
        )
        try:
            return None if row is None else QuoteEmployeeFact.model_validate(dict(row))
        except Exception:  # noqa: BLE001 - 持久字段损坏不枚举内容
            raise QuoteEvidenceError("source_unavailable") from None

    async def read_message(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> QuoteMessageReferenceFact | None:
        """两边显式tenant过滤，仅消息/会话引用字段，无body。"""
        _identity(tenant_id, message_id, "msg")
        row = await self._one(
            select(
                MessageRow.tenant_id,
                MessageRow.message_id,
                MessageRow.conversation_id,
                ConversationRow.account_id,
                ConversationRow.channel,
                MessageRow.direction,
                MessageRow.raw_artifact_ref.label("artifact_id"),
            )
            .join(
                ConversationRow,
                (MessageRow.tenant_id == ConversationRow.tenant_id)
                & (MessageRow.conversation_id == ConversationRow.conversation_id),
            )
            .where(
                MessageRow.tenant_id == tenant_id,
                ConversationRow.tenant_id == tenant_id,
                MessageRow.message_id == message_id,
            )
        )
        try:
            return (
                None
                if row is None
                else QuoteMessageReferenceFact.model_validate(dict(row))
            )
        except Exception:  # noqa: BLE001 - 非法绑定固定不可用
            raise QuoteEvidenceError("source_unavailable") from None

    async def read_need_quantity(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedQuantitySourceFact | None:
        """仅读完整quantity事实，不读取缺unit报价上下文或其他Need字段。"""
        _identity(tenant_id, need_id, "need")
        row = await self._one(
            select(
                ValidatedNeedRow.tenant_id,
                ValidatedNeedRow.need_id,
                ValidatedNeedRow.account_id,
                ValidatedNeedRow.quantity,
            ).where(
                ValidatedNeedRow.tenant_id == tenant_id,
                ValidatedNeedRow.need_id == need_id,
            )
        )
        try:
            return (
                None
                if row is None
                else NeedQuantitySourceFact.model_validate_json(json.dumps(dict(row)))
            )
        except Exception:  # noqa: BLE001 - JSON/Provenance错误不泄露源字段
            raise QuoteEvidenceError("source_unavailable") from None
