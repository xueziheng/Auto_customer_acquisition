"""新版报价namespace及审批本域请求身份，完整载荷验证由注入适配器完成。"""

import json
import re

from pydantic import JsonValue
from pydantic import ValidationError as PydanticValidationError

from domains.approvals.errors import QuoteContractError
from domains.approvals.models import ApprovalPackage
from domains.approvals.schemas import ApprovalQuoteSubject
from shared.schemas.identifiers import ApprovalId, EmployeeId, TenantId
from shared.schemas.quote_creation import (
    canonical_creation_hash,
    canonical_creation_value,
)


def quote_request_hash(package: ApprovalPackage) -> str:
    """原始请求身份含原limit与完整安全载荷，不受生成ID或重试时钟影响。"""
    value = {
        "version": "quote-approval-submit-v1",
        **{
            name: getattr(package, name)
            for name in (
                "tenant_id",
                "approval_type",
                "title",
                "proposed_change",
                "reason",
                "blast_radius",
                "proposed_by_run",
                "proposed_by_employee",
                "owner_employee",
                "evidence_refs",
                "change_set_ref",
                "expires_at_limit",
            )
        },
    }
    rendered = json.dumps(
        canonical_creation_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(rendered.encode("utf-8")) > 64_000:
        raise QuoteContractError("quote_contract_invalid")
    return canonical_creation_hash(value)


_TYPES = (
    "quote_send",
    "margin_floor_override",
    "discount",
    "delivery_commitment",
    "payment_terms",
    "certification_commitment",
)
_REF = re.compile(
    r"quote:(quo_[0-9A-HJKMNP-TV-Z]{26}):([0-9a-f]{64}):(" + "|".join(_TYPES) + ")"
)


def validate_quote_change_set_ref(value: str) -> None:
    """内部历史查找也只接受完整新版引用，不回退legacy查询。"""
    if not isinstance(value, str) or _REF.fullmatch(value) is None:
        raise QuoteContractError("quote_contract_invalid")


def quote_contract_subject(
    *,
    tenant_id: TenantId,
    approval_id: ApprovalId | None,
    approval_type: str,
    change_set_ref: str | None,
    proposed_change: dict[str, JsonValue],
    proposed_by_employee: EmployeeId | None,
    owner_employee: EmployeeId | None,
) -> ApprovalQuoteSubject | None:
    """任一疑似标记即fail-closed；无标记才按legacy处理。"""
    schema = proposed_change.get("schema_version")
    marked = (
        isinstance(change_set_ref, str)
        and change_set_ref.strip().casefold().startswith("quote:")
    ) or (
        isinstance(schema, str)
        and schema.strip().casefold().startswith("quote-approval")
    )
    if not marked:
        return None
    match = _REF.fullmatch(change_set_ref or "")
    if (
        match is None
        or schema != "quote-approval-v1"
        or approval_type not in _TYPES
        or match[3] != approval_type
        or proposed_change.get("approval_type") != approval_type
        or proposed_change.get("tenant_id") != tenant_id
        or proposed_change.get("quote_id") != match[1]
        or proposed_change.get("content_hash") != match[2]
        or proposed_change.get("prepared_by") != proposed_by_employee
        or proposed_change.get("submitted_owner_id") != owner_employee
        or type(proposed_change.get("quote_version")) is not int
        or proposed_change["quote_version"] < 1
        or proposed_by_employee is None
        or owner_employee is None
    ):
        raise QuoteContractError("quote_contract_invalid")
    try:
        return ApprovalQuoteSubject(
            tenant_id=tenant_id,
            approval_id=approval_id,
            approval_type=approval_type,
            change_set_ref=change_set_ref,
            quote_id=match[1],
            quote_version=proposed_change["quote_version"],
            content_hash=match[2],
            opportunity_id=proposed_change.get("opportunity_id"),
            prepared_by=proposed_by_employee,
            submitted_owner_id=owner_employee,
        )
    except PydanticValidationError:
        raise QuoteContractError("quote_contract_invalid") from None
