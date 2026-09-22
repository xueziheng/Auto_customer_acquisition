"""单位专用tenant-bound存储：严格解码原始JSON，不沿用旧数量int强转。"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as SchemaError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domains.demand.errors import NeedUnitUnavailableError
from domains.demand.schemas import (
    NeedCatalogEventLocator,
    NeedQuoteFacts,
    NeedUnitConfirmationView,
    NeedUnitStoredConfirmation,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    NeedUnitConfirmationRow,
    ValidatedNeedFieldHistoryRow,
    ValidatedNeedRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import NeedClusterId, TenantId, ValidatedNeedId, new_id
from shared.schemas.provenance import FactualField

_audit = logging.getLogger("infra.db.audit")
_FACT_FIELDS = (
    "product_category",
    "application",
    "material",
    "size_spec",
    "quantity",
    "packaging",
    "destination",
    "required_by",
    "target_price",
    "current_supply_issue",
    "certification_required",
    "unit",
)


def _validate_raw(value: object) -> None:
    """禁止非JSON对象值、数字时间和非字符串金额被Pydantic解码成可信事实。"""
    if isinstance(value, dict):
        for name, item in value.items():
            if (
                name in {"extracted_at", "confirmed_at", "observed_at"}
                and item is not None
                and type(item) is not str
            ):
                raise ValueError
            if name == "amount" and type(item) is not str:
                raise ValueError
            _validate_raw(item)
    elif isinstance(value, list):
        for item in value:
            _validate_raw(item)
    elif value is not None and type(value) not in (str, int, bool, float):
        raise ValueError


def _decode[T: BaseModel](kind: type[T], payload: dict[str, Any]) -> T:
    """严格JSON模式仅转换合法日期/Decimal/枚举，损坏存储统一脱敏。"""
    try:
        _validate_raw(payload)
        result = kind.model_validate_json(json.dumps(payload), strict=True)
        return result
    except (SchemaError, ValidationError, TypeError, ValueError, KeyError):
        raise NeedUnitUnavailableError("facts_corrupt") from None


def _facts(row: ValidatedNeedRow) -> NeedQuoteFacts:
    """直接投影原始JSON，不能经过旧仓储int/str修正。"""
    return _decode(
        NeedQuoteFacts,
        {
            "tenant_id": row.tenant_id,
            "need_id": row.need_id,
            "account_id": row.account_id,
            "status": row.status,
            "unit_quantity_fact_hash": row.unit_quantity_fact_hash,
            "unit_confirmation_id": row.unit_confirmation_id,
            **{name: getattr(row, name) for name in _FACT_FIELDS},
        },
    )


class NeedUnitRepositoryImpl(TenantScopedRepository):
    """查询统一注入租户；写入仍显式过滤tenant和need。"""

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        """绑定会话及租户，不读取环境。"""
        super().__init__(tenant_id)
        self._session = session

    def _tenant(self, tenant_id: TenantId) -> None:
        """不匹配参数必须安全审计并报告隔离违规。"""
        if tenant_id != self._tenant_id:
            _audit.critical(
                "检测到跨租户数据隔离违规", extra={"tenant_id": str(self._tenant_id)}
            )
            raise TenantIsolationViolation("跨租户数据隔离违规")

    async def read_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedQuoteFacts | None:
        """短事务完整读取，不锁定来源IO。"""
        self._tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(ValidatedNeedRow).where(
                    ValidatedNeedRow.need_id == need_id
                )
            )
        ).scalar_one_or_none()
        return _facts(row) if row else None

    async def lock_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedQuoteFacts | None:
        """锁定Need当前行，随后由域重验乐观绑定。"""
        self._tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(ValidatedNeedRow)
                .where(ValidatedNeedRow.need_id == need_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _facts(row) if row else None

    async def read_event_locator(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedCatalogEventLocator | None:
        """租户过滤后仅选目录事件定位列，不读取任何事实 JSON。"""
        self._tenant(tenant_id)
        row = (
            await self._session.execute(
                select(ValidatedNeedRow.need_id, ValidatedNeedRow.cluster_id).where(
                    ValidatedNeedRow.tenant_id == self._tenant_id,
                    ValidatedNeedRow.need_id == need_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return NeedCatalogEventLocator(
            need_id=ValidatedNeedId(row.need_id),
            cluster_id=(
                NeedClusterId(row.cluster_id) if row.cluster_id is not None else None
            ),
        )

    def _view(self, row: NeedUnitConfirmationRow) -> NeedUnitConfirmationView:
        """不可变payload必须与索引/外键列完全一致。"""
        result = _decode(NeedUnitConfirmationView, row.payload)
        if (
            result.tenant_id,
            result.need_id,
            result.confirmation_id,
            result.quantity_fact_hash,
            result.source.artifact_id,
            result.source.source_message_id,
            result.confirmed_by,
            result.confirmed_at,
        ) != (
            row.tenant_id,
            row.need_id,
            row.confirmation_id,
            row.quantity_fact_hash,
            row.artifact_id,
            row.source_message_id,
            row.confirmed_by,
            row.confirmed_at,
        ):
            raise NeedUnitUnavailableError("facts_corrupt")
        return result

    async def find_operation(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, idempotency_key: str
    ) -> NeedUnitStoredConfirmation | None:
        """幂等键仅在同租户同Need查找。"""
        self._tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(NeedUnitConfirmationRow).where(
                    NeedUnitConfirmationRow.need_id == need_id,
                    NeedUnitConfirmationRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return NeedUnitStoredConfirmation(
            view=self._view(row),
            idempotency_key=row.idempotency_key,
            request_hash=row.request_hash,
        )

    async def get_confirmation(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, confirmation_id: str
    ) -> NeedUnitConfirmationView | None:
        """历史确认需匹配完整复合身份。"""
        self._tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(NeedUnitConfirmationRow).where(
                    NeedUnitConfirmationRow.need_id == need_id,
                    NeedUnitConfirmationRow.confirmation_id == confirmation_id,
                )
            )
        ).scalar_one_or_none()
        return self._view(row) if row else None

    async def add_confirmation(
        self, tenant_id: TenantId, record: NeedUnitStoredConfirmation
    ) -> None:
        """先flush确认，再更新Need即可使用立即复合外键。"""
        self._tenant(tenant_id)
        self._tenant(record.view.tenant_id)
        view = record.view
        self._session.add(
            NeedUnitConfirmationRow(
                tenant_id=tenant_id,
                need_id=view.need_id,
                confirmation_id=view.confirmation_id,
                artifact_id=view.source.artifact_id,
                source_message_id=view.source.source_message_id,
                confirmed_by=view.confirmed_by,
                confirmed_at=view.confirmed_at,
                quantity_fact_hash=view.quantity_fact_hash,
                idempotency_key=record.idempotency_key,
                request_hash=record.request_hash,
                payload=view.model_dump(mode="json"),
            )
        )
        await self._session.flush()

    async def apply_current_unit(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        confirmation: NeedUnitConfirmationView,
    ) -> None:
        """仅写三单位列，不碰旧quantity与来源。"""
        self._tenant(tenant_id)
        self._tenant(confirmation.tenant_id)
        await self._session.execute(
            update(ValidatedNeedRow)
            .where(
                ValidatedNeedRow.tenant_id == self._tenant_id,
                ValidatedNeedRow.need_id == need_id,
            )
            .values(
                unit=confirmation.model_dump(mode="json")["unit"],
                unit_quantity_fact_hash=confirmation.quantity_fact_hash,
                unit_confirmation_id=confirmation.confirmation_id,
            )
        )

    async def append_unit_history(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        previous_unit: FactualField[str] | None,
        confirmation: NeedUnitConfirmationView,
    ) -> None:
        """历史与确认时间一致；完整来源永久留在receipt。"""
        self._tenant(tenant_id)
        self._tenant(confirmation.tenant_id)
        self._session.add(
            ValidatedNeedFieldHistoryRow(
                tenant_id=tenant_id,
                history_id=new_id("vh"),
                need_id=need_id,
                field_name="unit",
                old_value=previous_unit.value if previous_unit else None,
                new_value=confirmation.unit.value,
                source_message_id=confirmation.source.source_message_id,
                changed_by=confirmation.confirmed_by,
                changed_at=confirmation.confirmed_at,
            )
        )
        await self._session.flush()
