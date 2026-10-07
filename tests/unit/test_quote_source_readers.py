"""整数/逐字单位必要条件与两个域reader的安全映射。"""

from dataclasses import replace
from hashlib import sha256

import pytest

from domains.demand.errors import (
    NeedUnitError,
    NeedUnitPermissionError,
    NeedUnitUnavailableError,
)
from domains.demand.schemas import NeedUnitEvidenceQuery
from domains.demand.service import quantity_fact_hash
from shared.schemas import evidence_read as e
from shared.schemas.provenance import FactualField, Provenance, SourceType
from tests.unit.test_quote_evidence_access import (
    ACCOUNT,
    ACTOR,
    ARTIFACT,
    CONVERSATION,
    MESSAGE,
    NEED,
    NOW,
    TENANT,
    Contexts,
    NeedAuthorizer,
)
from tests.unit.test_quote_evidence_contracts import raw_meta

BODY = "We need 50 pieces.\n"


def quantity(**changes):
    return FactualField(
        50,
        Provenance(
            SourceType.CONVERSATION,
            MESSAGE,
            "human",
            NOW,
            confirmed_by=ACTOR,
            confirmed_at=NOW,
            source_quote="We need 50 pieces.",
            **changes,
        ),
    )


def facts_and_query(excerpt="50 pieces", *, body=None, start=None):
    field = quantity()
    if body is None:
        body = BODY if excerpt == "50 pieces" else BODY + excerpt
        start = 8 if excerpt == "50 pieces" else len(BODY)
    else:
        field = replace(field, provenance=replace(field.provenance, source_quote=body))
    assert start is not None
    current = e.NeedQuantitySourceFact(
        tenant_id=TENANT, need_id=NEED, account_id=ACCOUNT, quantity=field
    )
    selection, _ = e.select_evidence_text(
        e.ParsedEvidenceText(profile="rfc822-plain-v1", page=None, text=body),
        start,
        start + len(excerpt),
        maximum_excerpt_bytes=8192,
    )
    query = NeedUnitEvidenceQuery(
        tenant_id=TENANT,
        need_id=NEED,
        account_id=ACCOUNT,
        actor_id=ACTOR,
        quantity=field,
        quantity_fact_hash=quantity_fact_hash(TENANT, NEED, field),
        unit="pieces",
        source_message_id=MESSAGE,
        locator=e.make_evidence_locator(selection),
        source_quote=excerpt,
    )
    return current, query


@pytest.mark.parametrize(
    "body,start",
    [
        ("We need 150 pieces.", 9),
        ("We need 50 piecesXYZ.", 8),
        ("50 pieces. We need 150 pieces.", 20),
        ("😀 We need 150 pieces.", 11),
    ],
)
def test_selected_original_token_cannot_be_clipped(body, start):
    from domains.demand.service import validate_need_unit_source_text

    assert body[start : start + 9] == "50 pieces"
    current, query = facts_and_query(body=body, start=start)
    with pytest.raises(NeedUnitError) as caught:
        validate_need_unit_source_text(query, current, body=body, excerpt="50 pieces")
    assert caught.value.code == "source_mismatch"


@pytest.mark.parametrize(
    "body,start", [("We need 50 pieces.", 8), ("😀 We need 50 pieces.", 10)]
)
def test_original_complete_token_uses_codepoint_coordinates(body, start):
    from domains.demand.service import validate_need_unit_source_text

    current, query = facts_and_query(body=body, start=start)
    validate_need_unit_source_text(query, current, body=body, excerpt="50 pieces")


@pytest.mark.parametrize(
    "locator",
    [
        "$",
        "not-a-locator",
        "pdf-text-v1:p=1;c=8:17;h=" + sha256(b"50 pieces").hexdigest(),
        "rfc822-plain-v1:c=8:99;h=" + sha256(b"50 pieces").hexdigest(),
        "rfc822-plain-v1:c=0:9;h=" + sha256(b"50 pieces").hexdigest(),
    ],
)
def test_original_selection_binding_is_required(locator):
    from domains.demand.service import validate_need_unit_source_text

    current, query = facts_and_query()
    query = query.model_copy(update={"locator": locator})
    with pytest.raises(NeedUnitError) as caught:
        validate_need_unit_source_text(query, current, body=BODY, excerpt="50 pieces")
    assert caught.value.code == "source_mismatch"


@pytest.mark.parametrize(
    "excerpt", ["50 pieces", "We need 50 pieces.", "50\npieces", "50\u00a0pieces"]
)
def test_necessary_integer_unit_subset(excerpt):
    from domains.demand.service import validate_need_unit_source_text

    current, query = facts_and_query(excerpt)
    validate_need_unit_source_text(query, current, body=BODY + excerpt, excerpt=excerpt)


@pytest.mark.parametrize(
    "excerpt",
    [
        "500 pieces",
        "5000 pieces",
        "50.0 pieces",
        "1,500 pieces",
        "5e1 pieces",
        "x50 pieces",
        "_50 pieces",
        "50x pieces",
        "50_pieces",
        "50pieces",
        "+50 pieces",
        "-50 pieces",
        ".50 pieces",
        "50, pieces",
        "050 pieces",
        "50 units",
        "50 Pieces",
        "50 piece",
        "50 pieces and 50 pieces",
        "50 pieces, 2 boxes",
        "50 pieces pieces",
        "50 pieces_extra",
        "50 pieces2",
        "50 piecesA",
        "50 pieces ٢",
        "５０ pieces",
        "50\u200bpieces",
    ],
)
def test_no_subnumber_alias_repeated_or_embedded_token(excerpt):
    from domains.demand.service import validate_need_unit_source_text

    current, query = facts_and_query(excerpt)
    with pytest.raises(NeedUnitError) as caught:
        validate_need_unit_source_text(
            query, current, body=BODY + excerpt, excerpt=excerpt
        )
    assert caught.value.code == "source_mismatch"


@pytest.mark.parametrize(
    "change",
    [
        "tenant",
        "need",
        "account",
        "query_hash",
        "provenance",
        "message",
        "unconfirmed",
        "original_quote",
        "exact_excerpt",
        "nonpositive",
        "bool",
    ],
)
def test_full_quantity_provenance_and_message_binding(change):
    from domains.demand.service import validate_need_unit_source_text

    current, query = facts_and_query()
    body, excerpt = BODY, "50 pieces"
    if change in {"tenant", "need", "account"}:
        current = current.model_copy(
            update={change + "_id": getattr(current, change + "_id")[:-1] + "2"}
        )
    elif change == "query_hash":
        query = query.model_copy(update={"quantity_fact_hash": "f" * 64})
    elif change == "provenance":
        current = current.model_copy(
            update={
                "quantity": replace(
                    current.quantity,
                    provenance=replace(
                        current.quantity.provenance, extracted_by="other"
                    ),
                )
            }
        )
    elif change == "message":
        query = query.model_copy(update={"source_message_id": MESSAGE[:-1] + "2"})
    elif change in {"unconfirmed", "original_quote", "nonpositive", "bool"}:
        q = current.quantity
        if change == "unconfirmed":
            q = replace(
                q,
                provenance=replace(q.provenance, confirmed_by=None, confirmed_at=None),
            )
        if change == "original_quote":
            q = replace(
                q,
                provenance=replace(q.provenance, source_quote="Absent original quote"),
            )
        if change == "nonpositive":
            q = replace(q, value=0)
        if change == "bool":
            q = replace(q, value=True)
        current = current.model_copy(update={"quantity": q})
        query = query.model_copy(
            update={
                "quantity": q,
                "quantity_fact_hash": quantity_fact_hash(TENANT, NEED, q)
                if change != "bool"
                else "f" * 64,
            }
        )
    else:
        excerpt = "We need 50 pieces."
    with pytest.raises(NeedUnitError) as caught:
        validate_need_unit_source_text(query, current, body=body, excerpt=excerpt)
    assert caught.value.code == "source_mismatch"


class Reader:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.error = None
        self.after = None

    async def read(self, tenant_id, request, *, actor_id):
        self.calls.append(request)
        if self.error:
            raise e.QuoteEvidenceError(self.error)
        if self.after:
            self.after()
        return self.result


def reader_case():
    from workflows.quote_approval.source_readers import GatewayNeedUnitEvidenceReader

    current, query = facts_and_query()
    scope = e.NeedUnitEvidenceScope(purpose="need_unit", need_id=NEED, action="confirm")
    reference = e.AuthorizedEvidenceReference(
        tenant_id=TENANT,
        actor_id=ACTOR,
        source_ref="message:" + MESSAGE,
        scope=scope,
        raw=raw_meta(kind="email_raw", mime_type="message/rfc822"),
        message_id=MESSAGE,
        conversation_id=CONVERSATION,
        account_id=ACCOUNT,
    )
    selection = e.parse_evidence_locator(query.locator)
    result = e.EvidenceTextResult(
        reference=reference,
        profile="rfc822-plain-v1",
        page=None,
        text=BODY,
        text_hash=sha256(BODY.encode()).hexdigest(),
        selection=selection,
        excerpt="50 pieces",
        locator=query.locator,
    )
    contexts, need = Contexts(), NeedAuthorizer()
    contexts.need = current

    class Access:
        def __init__(self):
            self.calls = []

        async def authorize(self, *args, **kwargs):
            self.calls.append(kwargs["scope"])
            return reference.model_copy(update={"scope": kwargs["scope"]})

    access = Access()
    reader = Reader(result)
    return (
        GatewayNeedUnitEvidenceReader(reader, access, contexts, need),
        reader,
        contexts,
        need,
        access,
        query,
    )


async def test_need_adapter_self_selects_scope_and_maps_only_verified_fields():
    adapter, reader, _contexts, need, _, query = reader_case()
    result = await adapter.read_verified(query)
    assert (
        result.quantity_fact_hash == query.quantity_fact_hash
        and result.source_quote == query.source_quote
    )
    request = reader.calls[0]
    assert request.scope.purpose == "need_unit" and request.scope.action == "confirm"
    assert set(request.model_dump()) == {"operation", "source_ref", "scope", "locator"}
    assert need.calls == [(TENANT, NEED, ACTOR, "confirm")] * 2
    assert result.artifact_id == ARTIFACT and result.account_id == ACCOUNT


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_need_quantity_changes_fail_without_claiming_confirmation(phase):
    adapter, reader, contexts, _, _, query = reader_case()

    def change():
        contexts.need = contexts.need.model_copy(
            update={"quantity": replace(contexts.need.quantity, value=51)}
        )

    if phase == "before":
        change()
    else:
        reader.after = change
    with pytest.raises(NeedUnitError) as caught:
        await adapter.read_verified(query)
    assert caught.value.code == "source_mismatch" and len(reader.calls) == (
        0 if phase == "before" else 1
    )


@pytest.mark.parametrize(
    "code,error,want",
    [
        ("permission_denied", NeedUnitPermissionError, "permission_denied"),
        ("source_unsupported", NeedUnitError, "source_unsupported"),
        ("parse_unsupported", NeedUnitError, "source_unsupported"),
        ("locator_mismatch", NeedUnitError, "source_mismatch"),
        ("source_integrity_failed", NeedUnitError, "source_mismatch"),
        ("parse_limit_exceeded", NeedUnitUnavailableError, "source_unavailable"),
        ("gateway_unavailable", NeedUnitUnavailableError, "source_unavailable"),
    ],
)
async def test_need_fixed_error_mapping(code, error, want):
    adapter, reader, _, _, _, query = reader_case()
    reader.error = code
    with pytest.raises(error) as caught:
        await adapter.read_verified(query)
    assert caught.value.code == want


async def test_history_only_metadata_and_ignores_current_quantity():
    adapter, reader, contexts, _, access, query = reader_case()
    verified = await adapter.read_verified(query)
    reader.calls.clear()
    contexts.need = contexts.need.model_copy(
        update={"quantity": replace(contexts.need.quantity, value=51)}
    )
    await adapter.authorize_reference(TENANT, NEED, ACTOR, verified)
    assert not reader.calls and access.calls[-1].action == "read"


async def test_pricing_reader_fixed_scope_and_safe_source_mapping():
    from tests.unit.test_quote_evidence_gateway import Access
    from workflows.quote_approval.source_readers import GatewayPricingEvidenceReader

    ref = Access().reference
    result = e.EvidenceTextResult(
        reference=ref,
        profile=None,
        page=None,
        text=None,
        text_hash=None,
        selection=None,
        excerpt=None,
        locator="$",
    )
    reader = Reader(result)
    mapped = await GatewayPricingEvidenceReader(reader).read_verified(
        TENANT, ref.source_ref, "$", actor_id=ACTOR
    )
    assert (
        mapped.source_type == "upload"
        and mapped.source_url is None
        and mapped.observed_at == ref.raw.observed_at
    )
    assert reader.calls[0].scope == e.PricingEvidenceScope(purpose="pricing")
    assert "text" not in mapped.model_dump()
