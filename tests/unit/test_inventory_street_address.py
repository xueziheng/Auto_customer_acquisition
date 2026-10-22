"""真实公开正文解析后，所在地证据不能由拆句或市场覆盖冒充。"""

from datetime import UTC, datetime

import pytest

from connectors.web_search.client import _VisibleTextParser
from domains.prospecting.public_evidence import ProjectionRejected, project_candidate


def parse(html):
    parser = _VisibleTextParser()
    parser.feed(html)
    return parser.text()


def candidate(html, **changes):
    return project_candidate(
        **(
            {
                "source_id": "company_websites",
                "source_url": "https://acme.example/contact",
                "text": parse("<h1>Acme</h1><p>tricycles</p>" + html),
                "content_hash": "a" * 64,
                "artifact_id": "art_test",
                "observed_at": datetime.now(UTC),
                "name": "Acme",
                "country": "NG",
                "product_tags": ("tricycles",),
                "public_emails": (),
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<p>We export to <span>Nigeria</span></p>", "We export to Nigeria"),
        ("<p>Ni<b>ger</b>ia &amp; <em>more</em></p>", "Nigeria & more"),
        ("<div>one <b> two</b><p>three</p>four</div>", "one two\nthree\nfour"),
        ("<ul><li>one</li><li>two<br>three</li></ul>", "one\ntwo\nthree"),
        ("<table><tr><td>one</td><td>two</td></tr></table>", "one\ntwo"),
        (
            (
                "<p>a<script>bad</script><style>bad</style><noscript>bad</noscript>"
                "<template>bad</template>b</p>"
            ),
            "ab",
        ),
        ("<p>open <b>inline", "open inline"),
    ],
)
def test_inline_characters_and_block_boundaries(html, expected):
    assert parse(html) == expected


@pytest.mark.parametrize(
    "html",
    [
        "<p>42 Market Road, Lagos, Nigeria</p>",
        "<p>42/44 Market Rd., Central District, Lagos, Nigeria.</p>",
        "<p>42-44 Market Street, Lagos, Nigeria</p>",
        "<p><b>Address:</b> 42 Market Road, Lagos, Nigeria</p>",
        "<p>Our address:</p><p>Lagos, Nigeria</p>",
        "<p>Office: Lagos, Nigeria</p>",
    ],
)
def test_complete_street_or_explicit_location(html):
    result = candidate(html)
    assert result.country == "NG"
    assert result.provenance.page_hash == "a" * 64
    assert result.provenance.source_id == "art_test"
    assert result.source_url == "https://acme.example/contact"


@pytest.mark.parametrize(
    "html",
    [
        "<p>Nigeria</p>",
        "<select><option>Nigeria</option></select>",
        "<p>We export to <span>Nigeria</span></p>",
        "<p>We export to<br>Nigeria</p>",
        "<p>We sell to</p><p>Address: Nigeria</p>",
        "<p>Customer depot:</p><p>42 Market Road, Lagos, Nigeria</p>",
        "<p>42 Market Road, Lagos, Nigeria</p><p>Our supplier address</p>",
        "<p>According to the news</p><p>42 Market Road, Lagos, Nigeria</p>",
        "<p>We may move to <span>42 Market Road, Lagos, Nigeria</span></p>",
        "<p>Planned office:</p><p>42 Market Road, Lagos, Nigeria</p>",
        "<p>Not our address:</p><p>42 Market Road, Lagos, Nigeria</p>",
        "<p>42 Market Road, Lagos, Nigeria</p><p>India</p>",
        "<p>42 Market Road, Lagos, Nigeria and India</p>",
        "<p>Market Road, Lagos, Nigeria</p>",
        "<p>42 Market, Lagos, Nigeria</p>",
        "<p>42 Market Road, Nigeria</p>",
        "<p>42 Market Road, 12345, Nigeria</p>",
        "<p>P.O. Box 42, Lagos, Nigeria</p>",
        "<p>42 Market Road</p><p>Lagos, Nigeria</p>",
        "<p>Customer address: Nigeria</p>",
        "<p>Office may open in Nigeria</p>",
        "<p>" + "x" * 1001 + "</p><p>42 Market Road, Lagos, Nigeria</p>",
        "<p>42 " + "Market " * 40 + "Road, Lagos, Nigeria</p>",
    ],
)
def test_non_address_or_explicit_counterevidence_is_rejected(html):
    with pytest.raises(ProjectionRejected) as rejected:
        candidate(html)
    assert rejected.value.reason == "country_address_unproven"


def test_parser_limit_never_returns_partial_address_or_skips_overlarge_block(
    monkeypatch,
):
    import connectors.web_search.client as module

    monkeypatch.setattr(module, "_MAX_TEXT_CHARACTERS", 50)
    from shared.errors import ValidationError

    with pytest.raises(ValidationError, match="公开页面正文超限"):
        parse("<p>Acme</p><p>" + "x" * 51 + "</p><p>Address: Nigeria</p>")
    assert parse("<p>" + "x" * 50 + "</p>") == "x" * 50
    with pytest.raises(ValidationError, match="公开页面正文超限"):
        parse("<p>" + "x" * 49 + "</p><p>y</p>")


@pytest.mark.parametrize(
    "country,name,address",
    [
        ("KE", "Kenya", "42 Cedar Avenue, Nairobi, Kenya"),
        ("SS", "South Sudan", "42 Cedar Lane, Juba, South Sudan"),
        (
            "CG",
            "Republic of the Congo",
            "42 Cedar Drive, Brazzaville, Republic of the Congo",
        ),
    ],
)
def test_complete_country_entities_are_not_suffixes(country, name, address):
    assert candidate("<p>" + address + "</p>", country=country).country == country
    with pytest.raises(ProjectionRejected):
        candidate("<p>" + address + "</p>", country="NG")


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"name": "Invented"}, "company_name_unproven"),
        (
            {"source_url": "https://directory.example/contact"},
            "company_domain_unproven",
        ),
        ({"product_tags": ("hardware",)}, "product_unproven"),
        ({"public_emails": ("invented@acme.example",)}, "email_unproven"),
    ],
)
def test_street_evidence_does_not_bypass_other_gates(change, reason):
    with pytest.raises(ProjectionRejected) as error:
        candidate("<p>42 Cedar Road, Lagos, Nigeria</p>", **change)
    assert error.value.reason == reason


@pytest.mark.parametrize("overflow", [False, True])
async def test_http_and_browser_derive_same_text_and_keep_raw_artifact(
    monkeypatch, overflow
):
    import asyncio
    import json
    from hashlib import sha256
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from artifact_store.store import RawArtifactKind, RawArtifactStore
    from connectors.playwright.public_page import BrowserPublicPageReader
    from connectors.web_search.client import WebSearchConnector
    from connectors.web_search.transport import PublicPageResponse
    from shared.schemas.identifiers import new_id

    html = "<html><title>Acme</title><p>tri<b>cycles</b></p><p>42 Cedar Road, Lagos, Nigeria</p></html>"
    if overflow:
        html += "<p>" + "x" * 200001 + " Not our address</p>"
    body = html.encode()
    tenant, user, artifact = new_id("tn"), new_id("usr"), new_id("art")
    meta = SimpleNamespace(
        tenant_id=tenant, content_hash=sha256(body).hexdigest(), artifact_id=artifact
    )
    artifacts = AsyncMock(spec=RawArtifactStore)
    artifacts.put.return_value = meta
    transport = SimpleNamespace(
        validate_url=AsyncMock(),
        fetch=AsyncMock(return_value=PublicPageResponse("https://acme.example", body)),
    )
    http = WebSearchConnector(SimpleNamespace(search=AsyncMock()), transport, artifacts)
    from shared.errors import ValidationError
    from shared.schemas.page_failure import PageReadFailure

    if overflow:
        with pytest.raises(ValidationError, match="公开页面正文超限"):
            await http.read_page(tenant, "https://acme.example", uploaded_by=user)
    else:
        first = await http.read_page(tenant, "https://acme.example", uploaded_by=user)
    frame = json.dumps(
        {
            "url": "https://acme.example",
            "html": html,
            "screenshot": "eA==",
            "trace": "trace",
        }
    ).encode()
    stdin = SimpleNamespace(
        write=lambda value: None, drain=AsyncMock(), close=lambda: None
    )
    process = SimpleNamespace(
        stdin=stdin,
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[frame, b""])),
        wait=AsyncMock(),
        returncode=0,
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    if overflow:
        with pytest.raises(PageReadFailure) as failure:
            await BrowserPublicPageReader(artifacts).read_page(
                tenant, "https://acme.example", user
            )
        assert failure.value.stage == "browser_art" and failure.value.reason == "failed"
    else:
        second = await BrowserPublicPageReader(artifacts).read_page(
            tenant, "https://acme.example", user
        )
        assert (
            first.text
            == second.text
            == "Acme\ntricycles\n42 Cedar Road, Lagos, Nigeria"
        )
        assert first.content_hash == second.content_hash == sha256(body).hexdigest()
        assert first.snapshot_artifact_ref == second.snapshot_artifact_ref == artifact
    snapshots = [
        call.args
        for call in artifacts.put.call_args_list
        if call.args[1] == RawArtifactKind.WEB_SNAPSHOT
    ]
    assert (
        snapshots
        == [(tenant, RawArtifactKind.WEB_SNAPSHOT, body, "text/html", user)] * 2
    )


def test_own_dealer_business_description_is_not_a_third_party_address():
    assert (
        candidate(
            "<p>Acme is a tricycle dealer</p><p>42 Cedar Road, Lagos, Nigeria</p>"
        ).country
        == "NG"
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "Our distributor:",
        "Partner office:",
        "Client location:",
        "Supplier headquarters:",
    ],
)
def test_explicit_third_party_address_context(prefix):
    with pytest.raises(ProjectionRejected):
        candidate("<p>" + prefix + "</p><p>42 Cedar Road, Lagos, Nigeria</p>")


@pytest.mark.parametrize(
    "value",
    ["Address: Nigeria?", "Office: possibly Nigeria", "Location: maybe Nigeria"],
)
def test_uncertain_location_label_is_not_a_fact(value):
    with pytest.raises(ProjectionRejected):
        candidate("<p>" + value + "</p>")


@pytest.mark.parametrize("overflow", ["single_block", "cumulative"])
def test_truncated_counterevidence_cannot_reach_projection(overflow):
    from shared.errors import ValidationError

    address = "42 Cedar Road, Lagos, Nigeria"
    if overflow == "single_block":
        html = "<p>" + address + "</p><p>" + "x" * 200001 + " Not our address</p>"
    else:
        remaining = 200000 - len(f"Acme\ntricycles\n\none\ntwo\nthree\n{address}")
        html = (
            "<p>"
            + "x" * remaining
            + "</p><p>one</p><p>two</p><p>three</p><p>"
            + address
            + "</p><p>Not our address</p>"
        )
    with pytest.raises(ValidationError, match="公开页面正文超限"):
        candidate(html)
