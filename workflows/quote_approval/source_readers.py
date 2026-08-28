"""来源工具结果到成本/客户单位域的显式适配，零直接外部IO。"""

from domains.costing.service import SourceEvidence
from domains.demand.service import (
    NeedUnitAuthorizer,
    NeedUnitError,
    NeedUnitEvidenceQuery,
    NeedUnitPermissionError,
    NeedUnitUnavailableError,
    VerifiedNeedUnitEvidence,
    quantity_fact_hash,
    validate_need_unit_source_text,
)
from shared.errors import PermissionDenied
from shared.evidence_read import (
    QuoteEvidenceAccess,
    QuoteEvidenceContextReader,
    QuoteEvidenceReader,
)
from shared.schemas.evidence_read import (
    EvidenceVerifyRequest,
    NeedQuantitySourceFact,
    NeedUnitEvidenceScope,
    PricingEvidenceScope,
    QuoteEvidenceError,
)
from shared.schemas.identifiers import EmployeeId, TenantId, ValidatedNeedId


class GatewayPricingEvidenceReader:
    def __init__(self, reader: QuoteEvidenceReader) -> None:
        self._reader = reader

    async def read_verified(
        self,
        tenant_id: TenantId,
        source_ref: str,
        locator: str,
        *,
        actor_id: EmployeeId,
    ) -> SourceEvidence:
        """自选pricing用途，映射实际原件元数据，不携正文或推断业务归属。"""
        try:
            request = EvidenceVerifyRequest(
                operation="verify",
                source_ref=source_ref,
                scope=PricingEvidenceScope(purpose="pricing"),
                locator=locator,
            )
        except ValueError:
            raise QuoteEvidenceError("invalid_input") from None
        result = await self._reader.read(tenant_id, request, actor_id=actor_id)
        reference = result.reference
        if (
            reference.tenant_id,
            reference.actor_id,
            reference.source_ref,
            reference.scope,
            result.locator,
        ) != (tenant_id, actor_id, source_ref, request.scope, locator):
            raise QuoteEvidenceError("locator_mismatch")
        return SourceEvidence(
            tenant_id=tenant_id,
            source_ref=source_ref,
            artifact_id=reference.raw.artifact_id,
            content_hash=reference.raw.content_hash,
            locator=locator,
            observed_at=reference.raw.observed_at,
            source_type="upload",
            source_url=None,
        )


def _need_error(
    error: QuoteEvidenceError,
) -> NeedUnitError | NeedUnitPermissionError | NeedUnitUnavailableError:
    """沿既有Need域错误形状，不扩展错误枚举或传自由消息。"""
    if error.code == "permission_denied":
        return NeedUnitPermissionError("permission_denied")
    if error.code in {"source_unsupported", "parse_unsupported"}:
        return NeedUnitError("source_unsupported")
    if error.code in {"invalid_input", "locator_mismatch", "source_integrity_failed"}:
        return NeedUnitError("source_mismatch")
    return NeedUnitUnavailableError("source_unavailable")


class GatewayNeedUnitEvidenceReader:
    def __init__(
        self,
        reader: QuoteEvidenceReader,
        access: QuoteEvidenceAccess,
        contexts: QuoteEvidenceContextReader,
        need_authorizer: NeedUnitAuthorizer,
    ) -> None:
        self._reader, self._access, self._contexts, self._need = (
            reader,
            access,
            contexts,
            need_authorizer,
        )

    async def read_verified(
        self, query: NeedUnitEvidenceQuery
    ) -> VerifiedNeedUnitEvidence:
        """先后重读权限与完整数量hash，再调用域纯函数核原话必要关系。"""
        try:
            current = await self._current(query)
            scope = NeedUnitEvidenceScope(
                purpose="need_unit", need_id=query.need_id, action="confirm"
            )
            request = EvidenceVerifyRequest(
                operation="verify",
                source_ref="message:" + query.source_message_id,
                scope=scope,
                locator=query.locator,
            )
            result = await self._reader.read(
                query.tenant_id, request, actor_id=query.actor_id
            )
            current = await self._current(query)
            reference = result.reference
            if (
                (
                    reference.tenant_id,
                    reference.actor_id,
                    reference.source_ref,
                    reference.scope,
                    reference.message_id,
                    reference.account_id,
                    result.locator,
                )
                != (
                    query.tenant_id,
                    query.actor_id,
                    request.source_ref,
                    scope,
                    query.source_message_id,
                    query.account_id,
                    query.locator,
                )
                or result.text is None
                or result.excerpt is None
            ):
                raise NeedUnitError("source_mismatch")
            validate_need_unit_source_text(
                query, current, body=result.text, excerpt=result.excerpt
            )
            assert (
                reference.account_id is not None
                and reference.message_id is not None
                and result.locator is not None
            )
            return VerifiedNeedUnitEvidence(
                tenant_id=query.tenant_id,
                need_id=query.need_id,
                account_id=reference.account_id,
                source_message_id=reference.message_id,
                artifact_id=reference.raw.artifact_id,
                content_hash=reference.raw.content_hash,
                locator=result.locator,
                source_quote=result.excerpt,
                unit=query.unit,
                quantity_fact_hash=query.quantity_fact_hash,
                observed_at=reference.raw.observed_at,
            )
        except QuoteEvidenceError as error:
            raise _need_error(error) from None
        except (NeedUnitError, NeedUnitPermissionError, NeedUnitUnavailableError):
            raise
        except PermissionDenied:
            raise NeedUnitPermissionError("permission_denied") from None
        except ValueError:
            raise NeedUnitError("source_mismatch") from None
        except Exception:  # noqa: BLE001 - 来源依赖异常不携原文
            raise NeedUnitUnavailableError("source_unavailable") from None

    async def _current(self, query: NeedUnitEvidenceQuery) -> NeedQuantitySourceFact:
        """授权与完整hash核对均在Gateway前后、无guard/业务锁。"""
        permission = await self._need.check(
            query.tenant_id, query.need_id, query.actor_id, action="confirm"
        )
        current = await self._contexts.read_need_quantity(
            query.tenant_id, query.need_id
        )
        if (
            permission.tenant_id,
            permission.need_id,
            permission.actor_id,
            permission.account_id,
        ) != (query.tenant_id, query.need_id, query.actor_id, query.account_id):
            raise NeedUnitPermissionError("permission_denied")
        if (
            current is None
            or current.quantity is None
            or (current.tenant_id, current.need_id, current.account_id)
            != (query.tenant_id, query.need_id, query.account_id)
            or quantity_fact_hash(query.tenant_id, query.need_id, current.quantity)
            != query.quantity_fact_hash
            or quantity_fact_hash(query.tenant_id, query.need_id, query.quantity)
            != query.quantity_fact_hash
        ):
            raise NeedUnitError("source_mismatch")
        return current

    async def authorize_reference(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        actor_id: EmployeeId,
        source: VerifiedNeedUnitEvidence,
    ) -> None:
        """历史只核当前metadata与receipt原件绑定，不读bytes、不比较或激活现单位。"""
        try:
            permission = await self._need.check(
                tenant_id, need_id, actor_id, action="read"
            )
            if (
                permission.tenant_id,
                permission.need_id,
                permission.actor_id,
                permission.account_id,
            ) != (tenant_id, need_id, actor_id, source.account_id):
                raise NeedUnitPermissionError("permission_denied")
            scope = NeedUnitEvidenceScope(
                purpose="need_unit", need_id=need_id, action="read"
            )
            reference = await self._access.authorize(
                tenant_id,
                "message:" + source.source_message_id,
                actor_id=actor_id,
                scope=scope,
            )
            if (source.tenant_id, source.need_id) != (tenant_id, need_id) or (
                reference.tenant_id,
                reference.actor_id,
                reference.scope,
                reference.source_ref,
                reference.message_id,
                reference.account_id,
                reference.raw.artifact_id,
                reference.raw.content_hash,
                reference.raw.observed_at,
            ) != (
                tenant_id,
                actor_id,
                scope,
                "message:" + source.source_message_id,
                source.source_message_id,
                source.account_id,
                source.artifact_id,
                source.content_hash,
                source.observed_at,
            ):
                raise NeedUnitError("source_mismatch")
        except QuoteEvidenceError as error:
            raise _need_error(error) from None
        except (NeedUnitError, NeedUnitPermissionError, NeedUnitUnavailableError):
            raise
        except PermissionDenied:
            raise NeedUnitPermissionError("permission_denied") from None
        except ValueError:
            raise NeedUnitError("source_mismatch") from None
        except Exception:  # noqa: BLE001 - 历史来源依赖异常安全关闭
            raise NeedUnitUnavailableError("source_unavailable") from None
