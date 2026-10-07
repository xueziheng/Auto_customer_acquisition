"""来源工具只交接同task一次性结果，分类失败不携原文。"""

import asyncio
from hashlib import sha256

import pytest

from shared.schemas import evidence_read as e
from shared.schemas.identifiers import new_id
from tests.unit.test_evidence_text_profiles import parse_limits
from tests.unit.test_quote_evidence_access import ACTOR, UPLOAD
from tests.unit.test_quote_evidence_contracts import TENANT, raw_meta
from tool_gateway.errors import ToolCallStatus as Status
from tool_gateway.errors import ToolErrorCategory as Category
from tool_gateway.errors import ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolCallResult,
    ToolInvocationState,
)

TEXT = "Controlled private evidence 50 pieces."


def request(operation="preview", **changes):
    common = {
        "source_ref": "upload:" + UPLOAD,
        "scope": e.PricingEvidenceScope(purpose="pricing"),
    }
    if operation == "verify":
        return e.EvidenceVerifyRequest(
            operation=operation, **common, **({"locator": "$"} | changes)
        )
    if operation == "locate":
        return e.EvidenceLocateRequest(
            operation=operation,
            **common,
            **(
                {
                    "profile": "pdf-text-v1",
                    "page": 1,
                    "start": 0,
                    "end": 10,
                    "expected_raw_hash": raw_meta().content_hash,
                    "expected_text_hash": sha256(TEXT.encode()).hexdigest(),
                }
                | changes
            ),
        )
    return e.EvidencePreviewRequest(
        operation=operation,
        **common,
        **({"profile": "pdf-text-v1", "page": 1} | changes),
    )


class Access:
    def __init__(self):
        self.reference = e.AuthorizedEvidenceReference(
            tenant_id=TENANT,
            actor_id=ACTOR,
            source_ref="upload:" + UPLOAD,
            scope=e.PricingEvidenceScope(purpose="pricing"),
            raw=raw_meta(),
            message_id=None,
            conversation_id=None,
            account_id=None,
        )
        self.calls = 0
        self.failure = None

    async def authorize(self, *args, **kwargs):
        self.calls += 1
        if self.failure:
            raise e.QuoteEvidenceError(self.failure)
        return self.reference


class Raw:
    def __init__(self):
        self.calls = 0
        self.after = None

    async def read(self, tenant_id, artifact_id, *, maximum_bytes):
        self.calls += 1
        assert maximum_bytes == 2097152
        if self.after:
            self.after()
        return e.EvidenceRawContent(meta=raw_meta(), content=b"abc")


class Parser:
    def __init__(self):
        self.calls = 0
        self.ready = True
        self.failure = None

    def capability(self):
        return e.EvidenceParserCapability(
            status="available" if self.ready else "unavailable",
            profile_version="evidence-worker-v1",
            limits_hash="a" * 64,
            runtime_hash="b" * 64,
            failure=None,
        )

    async def parse(self, content, *, profile, page):
        self.calls += 1
        if self.failure:
            raise e.QuoteEvidenceError(self.failure)
        return e.ParsedEvidenceText(profile=profile, page=page, text=TEXT)


@pytest.fixture
def case():
    from tool_gateway.handlers.quote_evidence import QuoteEvidenceReadHandler
    from tool_gateway.handlers.quote_evidence_slots import QuoteEvidenceResultSlot

    access, raw, parser = Access(), Raw(), Parser()
    slot = QuoteEvidenceResultSlot(new_id)
    handler = QuoteEvidenceReadHandler(
        access,
        raw,
        parser,
        slot,
        HmacFingerprintProvider("evidence-test", b"x" * 32),
        maximum_raw_bytes=2097152,
        parser_limits=parse_limits(),
    )
    return handler, access, raw, parser, slot


def context(req=None, **changes):
    req = req or request()
    return ToolCallContext(
        **(
            {
                "tenant_id": TENANT,
                "user_id": ACTOR,
                "tool_id": "quotation.evidence.read",
                "params": req.model_dump(),
            }
            | changes
        )
    )


async def test_prepare_is_metadata_only_and_full_request_fingerprint(case):
    handler, access, raw, parser, _ = case
    first = await handler.prepare(context(), access.reference)
    second = await handler.prepare(context(request(page=2)), access.reference)
    assert first.request_fingerprint != second.request_fingerprint
    assert raw.calls == parser.calls == 0
    assert "locator" not in first.audit_projection and TEXT not in repr(first)
    for changes in [
        {"end": 11},
        {"expected_raw_hash": "0" * 64},
        {"expected_text_hash": "1" * 64},
    ]:
        base = await handler.prepare(context(request("locate")), access.reference)
        changed = await handler.prepare(
            context(request("locate", **changes)), access.reference
        )
        assert base.request_fingerprint != changed.request_fingerprint


@pytest.mark.parametrize("operation", ["preview", "locate", "verify"])
async def test_execute_reauth_and_single_result(case, operation):
    handler, access, raw, parser, slot = case
    prepared = await handler.prepare(context(request(operation)), access.reference)
    output = await handler.execute(TENANT, prepared)
    assert set(output) == {"provider_ref"} and output["provider_ref"].startswith("qev_")
    result = slot.take(output["provider_ref"])
    assert result.reference == access.reference and slot.is_empty
    assert raw.calls == 1 and parser.calls == (0 if operation == "verify" else 1)
    assert result.locator == "$" if operation == "verify" else result.text == TEXT
    with pytest.raises(e.QuoteEvidenceError):
        slot.take(output["provider_ref"])


async def test_root_does_not_require_parser_and_preview_does(case):
    handler, access, raw, parser, slot = case
    parser.ready = False
    await handler.prepare(context(request("verify")), access.reference)
    with pytest.raises(ToolGatewayError) as caught:
        await handler.prepare(context(), access.reference)
    assert caught.value.category is Category.PROVIDER_PERMANENT
    assert slot.take_failure() == "parse_unavailable" and raw.calls == 0


async def test_metadata_size_rejected_in_prepare(case):
    handler, access, raw, _, slot = case
    access.reference = access.reference.model_copy(
        update={"raw": raw_meta(size_bytes=2097153)}
    )
    with pytest.raises(ToolGatewayError):
        await handler.prepare(context(), access.reference)
    assert slot.take_failure() == "source_limit_exceeded" and raw.calls == 0


@pytest.mark.parametrize("phase", ["prepare", "execute"])
async def test_revoke_or_replace_current_binding(case, phase):
    handler, access, raw, _, slot = case
    if phase == "prepare":
        original = access.reference
        access.reference = original.model_copy(
            update={"raw": raw_meta(content_hash="f" * 64)}
        )
        with pytest.raises(ToolGatewayError):
            await handler.prepare(context(), original)
    else:
        prepared = await handler.prepare(context(), access.reference)
        raw.after = lambda: setattr(access, "failure", "permission_denied")
        with pytest.raises(ToolGatewayError):
            await handler.execute(TENANT, prepared)
    assert slot.take_failure() == "permission_denied"


@pytest.mark.parametrize(
    "code", ["parse_unsupported", "parse_timeout", "parse_limit_exceeded"]
)
async def test_execute_keeps_parser_failure_code(case, code):
    handler, access, _, parser, slot = case
    prepared = await handler.prepare(context(), access.reference)
    parser.failure = code
    with pytest.raises(ToolGatewayError):
        await handler.execute(TENANT, prepared)
    assert slot.take_failure() == code


async def test_locator_stale_hash_is_not_success(case):
    handler, access, _, _, slot = case
    prepared = await handler.prepare(
        context(request("locate", expected_text_hash="0" * 64)), access.reference
    )
    with pytest.raises(ToolGatewayError):
        await handler.execute(TENANT, prepared)
    assert slot.take_failure() == "locator_mismatch"


async def test_slot_task_ownership_and_failure_never_takes_success(case):
    handler, access, _, _, slot = case
    output = await handler.execute(
        TENANT, await handler.prepare(context(), access.reference)
    )
    handle = output["provider_ref"]
    assert slot.take_failure() is None and not slot.is_empty

    async def child():
        assert slot.is_empty
        with pytest.raises(e.QuoteEvidenceError):
            slot.take(handle)
        slot.discard_all()

    await asyncio.create_task(child())
    assert slot.take(handle).text == TEXT and slot.is_empty
    slot.put_failure("source_unsupported")
    assert slot.take_failure() == "source_unsupported" and slot.is_empty


@pytest.mark.parametrize("code", ["source_unsupported", "source_unavailable"])
async def test_permission_stage_preserves_classified_failure(case, code):
    from tool_gateway.checks.quote_evidence import QuoteEvidencePermissionCheck
    from tool_gateway.handlers.quote_evidence import MANIFEST

    _, access, raw, _, slot = case
    access.failure = code
    with pytest.raises(ToolGatewayError):
        await QuoteEvidencePermissionCheck(access, slot).check(
            context(), ToolInvocationState(MANIFEST, new_id("tcl"))
        )
    assert slot.take_failure() == code and raw.calls == 0


@pytest.mark.parametrize(
    "changes,rule",
    [
        ({"user_id": " invalid"}, "tenant:evidence_request"),
        (
            {"params": dict(request().model_dump(), role="boss")},
            "tenant:evidence_request",
        ),
        ({"tenant_id": TENANT[:-1] + "2"}, "tenant:evidence_tenant"),
    ],
)
async def test_tenant_check_exact_rules(changes, rule):
    from tool_gateway.checks.quote_evidence import QuoteEvidenceTenantCheck

    result = await QuoteEvidenceTenantCheck(TENANT).check(context(**changes), None)
    assert result.rule == rule


class Invoker:
    def __init__(self, result):
        self.result = result
        self.effect = None

    async def invoke(self, ctx):
        assert ctx.user_id == ACTOR and "quantity" not in ctx.params
        if self.effect:
            self.effect()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    "category,rule,want",
    [
        (Category.VALIDATION, None, "invalid_input"),
        (Category.PERMISSION_DENIED, None, "permission_denied"),
        (Category.PROVIDER_TRANSIENT, None, "source_unavailable"),
        (Category.PROVIDER_PERMANENT, None, "gateway_unavailable"),
        (None, "tenant:evidence_request", "invalid_input"),
        (None, "tenant:evidence_tenant", "permission_denied"),
        (None, "tenant:unknown", "gateway_unavailable"),
        (None, None, "gateway_unavailable"),
    ],
)
async def test_wrapper_fixed_fallback(case, category, rule, want):
    from tool_gateway.handlers.quote_evidence import ToolGatewayQuoteEvidenceReader

    _, access, _, _, slot = case
    result = ToolCallResult(
        "quotation.evidence.read",
        Status.REJECTED,
        error_category=category,
        rejected=CheckRejection("tenant", rule or "tenant:unknown", "固定拒绝"),
    )
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await ToolGatewayQuoteEvidenceReader(Invoker(result), slot, access).read(
            TENANT, request(), actor_id=ACTOR
        )
    assert caught.value.code == want and slot.is_empty


@pytest.mark.parametrize(
    "outcome", ["success", "audit_failed", "invoke_raised", "cancelled", "duplicate"]
)
async def test_wrapper_never_delivers_content_after_audit_failure(case, outcome):
    from tool_gateway.handlers.quote_evidence import ToolGatewayQuoteEvidenceReader

    handler, access, _, _, slot = case
    output = await handler.execute(
        TENANT, await handler.prepare(context(), access.reference)
    )
    payload = slot.take(output["provider_ref"])
    invoker = Invoker(
        ToolCallResult(
            "quotation.evidence.read",
            Status.SUCCEEDED,
            output={"provider_ref": "qev_" + UPLOAD[4:]},
        )
    )

    def effect():
        handle = slot.put(payload)
        invoker.result = ToolCallResult(
            "quotation.evidence.read", Status.SUCCEEDED, output={"provider_ref": handle}
        )
        if outcome == "audit_failed":
            invoker.result = ToolCallResult(
                "quotation.evidence.read",
                Status.FAILED_PERMANENT,
                error_category=Category.UNEXPECTED,
            )
        if outcome == "invoke_raised":
            invoker.result = RuntimeError("controlled")
        if outcome == "cancelled":
            invoker.result = asyncio.CancelledError()
        if outcome == "duplicate":
            invoker.result = ToolCallResult(
                "quotation.evidence.read", Status.DUPLICATE, duplicate_of=new_id("tcl")
            )

    invoker.effect = effect
    wrapper = ToolGatewayQuoteEvidenceReader(invoker, slot, access)
    if outcome == "success":
        assert (await wrapper.read(TENANT, request(), actor_id=ACTOR)).text == TEXT
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await wrapper.read(TENANT, request(), actor_id=ACTOR)
    else:
        with pytest.raises(e.QuoteEvidenceError) as caught:
            await wrapper.read(TENANT, request(), actor_id=ACTOR)
        assert caught.value.code == "gateway_unavailable"
    assert slot.is_empty


async def test_nested_wrapper_does_not_clear_outer_slot(case):
    from tool_gateway.handlers.quote_evidence import ToolGatewayQuoteEvidenceReader

    handler, access, _, _, slot = case
    output = await handler.execute(
        TENANT, await handler.prepare(context(), access.reference)
    )
    with pytest.raises(e.QuoteEvidenceError):
        await ToolGatewayQuoteEvidenceReader(Invoker(None), slot, access).read(
            TENANT, request(), actor_id=ACTOR
        )
    assert slot.take(output["provider_ref"]).text == TEXT


async def test_unclassified_parser_failure_is_fixed_parse_unavailable(
    case, monkeypatch
):
    handler, access, _, parser, slot = case

    async def fail(*args, **kwargs):
        raise RuntimeError("controlled parse detail")

    monkeypatch.setattr(parser, "parse", fail)
    prepared = await handler.prepare(context(), access.reference)
    with pytest.raises(ToolGatewayError):
        await handler.execute(TENANT, prepared)
    assert slot.take_failure() == "parse_unavailable"


async def test_raw_bytes_are_rechecked_at_handler_boundary(case, monkeypatch):
    handler, access, raw, _, slot = case

    async def forged(*args, **kwargs):
        return e.EvidenceRawContent(meta=raw_meta(), content=b"abc").model_copy(
            update={"content": b"abd"}
        )

    monkeypatch.setattr(raw, "read", forged)
    prepared = await handler.prepare(context(), access.reference)
    with pytest.raises(ToolGatewayError):
        await handler.execute(TENANT, prepared)
    assert slot.take_failure() == "source_integrity_failed"


async def test_none_parser_allows_only_verified_root(case):
    handler, access, raw, _, slot = case
    handler._parser = None
    prepared = await handler.prepare(context(request("verify")), access.reference)
    output = await handler.execute(TENANT, prepared)
    assert slot.take(output["provider_ref"]).text is None and raw.calls == 1
    with pytest.raises(ToolGatewayError):
        await handler.prepare(context(), access.reference)
    assert slot.take_failure() == "parse_unavailable"


async def test_wrapper_keeps_failure_code_and_final_reauth(case):
    from tool_gateway.handlers.quote_evidence import ToolGatewayQuoteEvidenceReader

    handler, access, _, _, slot = case
    result = ToolCallResult(
        "quotation.evidence.read",
        Status.FAILED_PERMANENT,
        error_category=Category.PROVIDER_PERMANENT,
    )
    invoker = Invoker(result)
    invoker.effect = lambda: slot.put_failure("source_unsupported")
    wrapper = ToolGatewayQuoteEvidenceReader(invoker, slot, access)
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await wrapper.read(TENANT, request(), actor_id=ACTOR)
    assert caught.value.code == "source_unsupported" and slot.is_empty
    output = await handler.execute(
        TENANT, await handler.prepare(context(), access.reference)
    )
    payload = slot.take(output["provider_ref"])

    def success_then_revoke():
        handle = slot.put(payload)
        invoker.result = ToolCallResult(
            "quotation.evidence.read", Status.SUCCEEDED, output={"provider_ref": handle}
        )
        access.failure = "permission_denied"

    invoker.effect = success_then_revoke
    with pytest.raises(e.QuoteEvidenceError) as caught:
        await wrapper.read(TENANT, request(), actor_id=ACTOR)
    assert caught.value.code == "permission_denied" and slot.is_empty


async def test_need_scope_action_and_need_id_are_hmac_bound(case):
    from tests.unit.test_quote_evidence_access import (
        ACCOUNT,
        CONVERSATION,
        MESSAGE,
        NEED,
    )

    handler, access, _, _, _ = case
    fingerprints = []
    for need_id, action in [
        (NEED, "read"),
        (NEED, "confirm"),
        (NEED[:-1] + "2", "read"),
    ]:
        scope = e.NeedUnitEvidenceScope(
            purpose="need_unit", need_id=need_id, action=action
        )
        access.reference = e.AuthorizedEvidenceReference(
            tenant_id=TENANT,
            actor_id=ACTOR,
            source_ref="message:" + MESSAGE,
            scope=scope,
            raw=raw_meta(kind="email_raw", mime_type="message/rfc822"),
            message_id=MESSAGE,
            conversation_id=CONVERSATION,
            account_id=ACCOUNT,
        )
        req = e.EvidencePreviewRequest(
            operation="preview",
            source_ref="message:" + MESSAGE,
            scope=scope,
            profile="rfc822-plain-v1",
            page=None,
        )
        fingerprints.append(
            (await handler.prepare(context(req), access.reference)).request_fingerprint
        )
    assert len(set(fingerprints)) == 3
