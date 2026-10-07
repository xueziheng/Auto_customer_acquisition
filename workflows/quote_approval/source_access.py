"""当前用途的资料授权编排，不读取资料bytes。"""

import re

from pydantic import TypeAdapter

from domains.conversations.service import require_inbound_source_access
from domains.costing.service import PriceEvidenceView, require_pricing_source_access
from domains.demand.service import NeedUnitAuthorizer
from shared.errors import PermissionDenied, ValidationError
from shared.evidence_read import (
    QuoteEvidenceAccess,
    QuoteEvidenceContextReader,
    QuoteEvidenceRawReader,
)
from shared.schemas.evidence_read import (
    AuthorizedEvidenceReference,
    EvidenceRawMeta,
    EvidenceScope,
    NeedUnitEvidenceScope,
    PricingEvidenceScope,
    QuoteEvidenceError,
    QuoteMessageReferenceFact,
)
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId, WorkUploadId
from shared.schemas.quote_facts import QuoteEmployeeFact, fact_identity
from workflows.employee_work_intake.service import WorkIntakeService


class CurrentCostScopeSourceAccess:
    """只重核持久依据的当前资料ACL/metadata绑定，不解析或发冻结票据。"""

    def __init__(self, access: QuoteEvidenceAccess) -> None:
        self._access = access

    async def require(
        self,
        tenant_id: TenantId,
        evidence: tuple[PriceEvidenceView, ...],
        *,
        actor_id: EmployeeId,
    ) -> None:
        scope = PricingEvidenceScope(purpose="pricing")
        for value in evidence:
            source = value.source
            if source.tenant_id != tenant_id or source.source_ref != value.source_ref:
                raise QuoteEvidenceError("source_integrity_failed")
            try:
                reference = await self._access.authorize(
                    tenant_id, source.source_ref, actor_id=actor_id, scope=scope
                )
            except QuoteEvidenceError as error:
                if error.code == "permission_denied":
                    raise PermissionDenied("当前无权读取来源") from None
                raise
            if (
                reference.tenant_id,
                reference.actor_id,
                reference.source_ref,
                reference.scope,
                reference.raw.tenant_id,
                reference.raw.artifact_id,
                reference.raw.content_hash,
            ) != (
                tenant_id,
                actor_id,
                source.source_ref,
                scope,
                tenant_id,
                source.artifact_id,
                source.content_hash,
            ):
                raise QuoteEvidenceError("source_integrity_failed")


class QuoteEvidenceAccessImpl:
    """每次重读当前身份、公开资料归属与元数据。"""

    def __init__(
        self,
        contexts: QuoteEvidenceContextReader,
        raw: QuoteEvidenceRawReader,
        uploads: WorkIntakeService,
        need_authorizer: NeedUnitAuthorizer,
    ) -> None:
        self._contexts, self._raw = contexts, raw
        self._uploads, self._need = uploads, need_authorizer

    async def authorize(
        self,
        tenant_id: TenantId,
        source_ref: str,
        *,
        actor_id: EmployeeId,
        scope: EvidenceScope,
    ) -> AuthorizedEvidenceReference:
        """先用途权限，再当前引用及元数据；无业务锁与原文IO。"""
        try:
            fact_identity(actor_id)
            if (
                type(tenant_id) is not str
                or re.fullmatch(r"tn_[0-7][0-9A-HJKMNP-TV-Z]{25}", tenant_id) is None
            ):
                raise ValueError
            if (
                type(source_ref) is not str
                or re.fullmatch(
                    r"(?:upload:upl_|message:msg_)[0-7][0-9A-HJKMNP-TV-Z]{25}",
                    source_ref,
                )
                is None
            ):
                raise ValueError
            scope = TypeAdapter(EvidenceScope).validate_python(
                scope.model_dump(), strict=True
            )
        except (ValueError, AttributeError):
            raise QuoteEvidenceError("invalid_input") from None
        try:
            current = await self._contexts.read_actor(tenant_id, actor_id)
            if current is None:
                raise QuoteEvidenceError("permission_denied")
            current = QuoteEmployeeFact.model_validate(current.model_dump())
            if (
                current.tenant_id != tenant_id
                or current.employee_id != actor_id
                or current.is_active is not True
            ):
                raise QuoteEvidenceError("permission_denied")
            if (scope.purpose == "pricing") != source_ref.startswith("upload:"):
                raise QuoteEvidenceError("source_unsupported")
            message = None
            if scope.purpose == "pricing":
                require_pricing_source_access(tenant_id, current)
                upload_id = WorkUploadId(source_ref.removeprefix("upload:"))
                try:
                    upload = await self._uploads.get_upload(
                        tenant_id, upload_id, actor_id
                    )
                except (PermissionDenied, ValidationError):
                    raise QuoteEvidenceError("permission_denied") from None
                if (upload.tenant_id, upload.upload_id, upload.employee_id) != (
                    tenant_id,
                    upload_id,
                    actor_id,
                ):
                    raise QuoteEvidenceError("permission_denied")
                artifact_id = upload.artifact_id
            else:
                assert isinstance(scope, NeedUnitEvidenceScope)
                if self._need is None:
                    raise QuoteEvidenceError("permission_denied")
                try:
                    permitted = await self._need.check(
                        tenant_id, scope.need_id, actor_id, action=scope.action
                    )
                except (PermissionDenied, ValidationError):
                    raise QuoteEvidenceError("permission_denied") from None
                if (permitted.tenant_id, permitted.need_id, permitted.actor_id) != (
                    tenant_id,
                    scope.need_id,
                    actor_id,
                ):
                    raise QuoteEvidenceError("permission_denied")
                message_id = MessageId(source_ref.removeprefix("message:"))
                message = await self._contexts.read_message(tenant_id, message_id)
                if message is None:
                    raise QuoteEvidenceError("permission_denied")
                message = QuoteMessageReferenceFact.model_validate(message.model_dump())
                require_inbound_source_access(tenant_id, current, message)
                if (
                    message.message_id != message_id
                    or message.account_id != permitted.account_id
                ):
                    raise QuoteEvidenceError("permission_denied")
                artifact_id = message.artifact_id
            raw = await self._raw.get_meta(tenant_id, artifact_id)
            raw = EvidenceRawMeta.model_validate(raw.model_dump())
            if (raw.tenant_id, raw.artifact_id) != (tenant_id, artifact_id):
                raise QuoteEvidenceError("permission_denied")
            if raw.kind != ("pdf" if scope.purpose == "pricing" else "email_raw"):
                raise QuoteEvidenceError("source_unsupported")
            return AuthorizedEvidenceReference(
                tenant_id=tenant_id,
                actor_id=actor_id,
                source_ref=source_ref,
                scope=scope,
                raw=raw,
                message_id=message.message_id if message else None,
                conversation_id=message.conversation_id if message else None,
                account_id=message.account_id if message else None,
            )
        except QuoteEvidenceError:
            raise
        except Exception:  # noqa: BLE001 - 依赖错误不暴露原文或连接
            raise QuoteEvidenceError("source_unavailable") from None
