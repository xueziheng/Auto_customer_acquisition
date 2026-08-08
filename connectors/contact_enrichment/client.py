"""联系人补全连接器骨架。实现 prospecting.EnrichmentProvider。"""

from __future__ import annotations

from typing import Any

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="contact_enrichment",
    capabilities=("contact.enrich",),
    secret_refs=("CONTACT_ENRICH_API_KEY_REF",),
    compliance_note="返回线索必须带来源；具名业务邮箱是个人数据",
)


class ContactEnrichmentConnector:
    manifest = MANIFEST

    async def configure(self, secret_resolver: Any) -> None:
        raise NotImplementedError

    async def health_check(self) -> bool:
        raise NotImplementedError

    async def find_contacts(
        self, company_domain: str, role_hints: list[str]
    ) -> list[dict]:
        """返回联系人线索（含 source、cost_note）。"""
        raise NotImplementedError
