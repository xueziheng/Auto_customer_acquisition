"""单个受控联系人Provider与结构化模型；仅返回具名外部fixture并独立记调用。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from connectors.contact_enrichment.client import (
    ContactCandidate,
    ContactEnrichmentResult,
    ContactSource,
    EnrichmentCostNote,
)
from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from shared.schemas.identifiers import TenantId

from .config import ControlledError


class ControlledContactProvider:
    """沿同owner mail数据库的调用表计每次动作；不按邮箱去重或写域结果。"""

    def __init__(
        self, path: Path, *, tenant_id: str, now: Callable[[], datetime]
    ) -> None:
        self._path, self._tenant, self._now = path, tenant_id, now

    def _record(self, operation: str) -> None:
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS controlled_contact_calls (tenant_id TEXT NOT NULL, call_id TEXT PRIMARY KEY, operation TEXT NOT NULL, recorded_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO controlled_contact_calls VALUES (?, ?, ?, ?)",
                (self._tenant, uuid4().hex, operation, self._now().isoformat()),
            )

    def list_calls(self) -> tuple[str, ...]:
        if not self._path.exists():
            return ()
        with closing(sqlite3.connect(self._path)) as db:
            exists = db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='controlled_contact_calls'"
            ).fetchone()[0]
            if not exists:
                return ()
            rows = db.execute(
                "SELECT operation FROM controlled_contact_calls WHERE tenant_id=? ORDER BY rowid",
                (self._tenant,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    async def find_contacts(
        self, tenant_id: TenantId, company_domain: str, role_hints: tuple[str, ...]
    ) -> ContactEnrichmentResult:
        if (
            tenant_id != self._tenant
            or company_domain != "controlled-outreach.example.com"
            or role_hints != ("procurement",)
        ):
            raise ControlledError("controlled_contact_input_rejected")
        self._record("contact.enrich")
        today = self._now().date()
        return ContactEnrichmentResult(
            (
                ContactCandidate(
                    "buyer0@example.test",
                    "Controlled Buyer",
                    "Procurement",
                    sources=(
                        ContactSource(
                            "https://controlled-outreach.example.com/about",
                            today,
                            today,
                            True,
                        ),
                    ),
                ),
            ),
            "controlled-single-provider",
            EnrichmentCostNote.COUNTED,
        )

    async def verify(self, tenant_id: TenantId, email: str) -> EmailVerificationResult:
        if tenant_id != self._tenant or email != "buyer0@example.test":
            raise ControlledError("controlled_contact_input_rejected")
        self._record("contact.verify")
        return EmailVerificationResult(
            EmailVerificationOutcome.VERIFIED,
            "controlled-single-provider",
            self._now(),
            VerificationCostNote.COUNTED,
        )


class ControlledAccountModel:
    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        organization = payload.get("organization")
        refs = payload.get("source_signal_refs")
        if (
            model != "controlled-account-v1"
            or max_output_tokens != 1500
            or set(payload)
            != {"hypothesis_id", "organization", "category", "source_signal_refs"}
            or not isinstance(organization, dict)
            or organization.get("website_domain") != "controlled-outreach.example.com"
            or organization.get("country") != "KE"
            or payload.get("category") != "hinges"
            or not isinstance(refs, tuple)
            or len(refs) != 1
            or not isinstance(refs[0], str)
            or not refs[0].startswith("sig_")
        ):
            raise ControlledError("controlled_account_model_input_rejected")
        return json.dumps(
            {"evidence_sufficient": True, "source_signal_refs": list(refs)}
        )
