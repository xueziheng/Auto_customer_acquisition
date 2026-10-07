"""Prospecting → Demand 的目录账户国家事实窄适配。"""

from __future__ import annotations

from domains.demand.service import (
    DemandCatalogAccountFact,
    catalog_country_code_or_none,
    catalog_evidence_summary,
)
from domains.prospecting.service import ProspectingService
from shared.errors import ValidationError, detached_dependency_error
from shared.schemas.identifiers import ProspectAccountId, TenantId
from shared.schemas.provenance import Provenance, SourceType


class ProspectingDemandCatalogAccountFactsReader:
    """只读取一个账户并逐字段映射安全国家事实。"""

    def __init__(self, prospecting: ProspectingService) -> None:
        if not callable(getattr(prospecting, "get_account", None)):
            raise ValidationError("目录账户事实依赖无效")
        self._prospecting = prospecting

    async def get_account_catalog_fact(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
    ) -> DemandCatalogAccountFact:
        """非法或证据不完整的国家返回未知；依赖故障使用固定安全错误。"""
        try:
            account = await self._prospecting.get_account(tenant_id, account_id)
            returned_tenant = getattr(account, "tenant_id", None)
            returned_account = getattr(account, "account_id", None)
            country = getattr(account, "country", None)
            field_provenance = getattr(account, "field_provenance", None)
            if (
                returned_tenant != tenant_id
                or returned_account != account_id
                or not isinstance(field_provenance, dict)
            ):
                raise ValidationError("目录账户事实结果无效")
            country_code = catalog_country_code_or_none(country)
            provenance = field_provenance.get("country")
            if (
                country_code is None
                or not isinstance(provenance, Provenance)
                or provenance.source_type is SourceType.AGENT_INFERENCE
            ):
                return DemandCatalogAccountFact(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    country_code=None,
                    country_evidence=None,
                )
            try:
                evidence = catalog_evidence_summary(
                    tenant_id=tenant_id,
                    subject_id=str(account_id),
                    field_name="country",
                    value=country_code,
                    provenance=provenance,
                )
            except (TypeError, ValueError):
                return DemandCatalogAccountFact(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    country_code=None,
                    country_evidence=None,
                )
            return DemandCatalogAccountFact(
                tenant_id=tenant_id,
                account_id=account_id,
                country_code=country_code,
                country_evidence=evidence,
            )
        except Exception as error:  # noqa: BLE001 - 跨域依赖必须固定分类并脱敏
            mapped = detached_dependency_error(
                error,
                transient_message="目录账户事实依赖暂不可用",
                permanent_message="目录账户事实依赖返回无效",
            )
            raise mapped from None


__all__ = ("ProspectingDemandCatalogAccountFactsReader",)
