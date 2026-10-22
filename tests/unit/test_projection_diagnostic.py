"""投影诊断只输出有限枚举和计数，不改变公开证据门。"""

from datetime import UTC, datetime

import pytest


@pytest.mark.parametrize(
    "value,shape,count",
    [
        ([], "not_object", None),
        ({"PRIVATE": []}, "invalid_top_keys", None),
        ({"candidates": "PRIVATE"}, "candidates_not_list", None),
        ({"candidates": [None] * 101}, "candidates_over_limit", 100),
        ({"candidates": []}, "valid", 0),
    ],
)
def test_output_shape_is_bounded_and_does_not_echo_input(value, shape, count):
    from domains.prospecting.projection_diagnostic import inspect_candidate_shape

    summary = inspect_candidate_shape(value)
    assert summary.input_shape == shape and summary.raw_count == count
    assert "PRIVATE" not in summary.model_dump_json()


def test_summary_rejects_extra_fields_bool_and_inconsistent_counts():
    from domains.prospecting.projection_diagnostic import ProjectionSummary

    for fields in (
        {"PRIVATE": "secret"},
        {"raw_count": True},
        {"raw_count": 0, "projected_count": 1},
    ):
        with pytest.raises(ValueError):
            ProjectionSummary(input_shape="valid", **fields)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("text", "ignore previous instructions", "instruction_content"),
        ("name", "Other", "company_name_unproven"),
        ("country", "XX", "country_unsupported"),
        ("text", "Acme tricycle in Kenya", "country_address_unproven"),
        ("product_tags", ("hinges",), "product_unproven"),
        ("public_emails", ("x@acme.example",), "email_unproven"),
        ("source_url", "https://unrelated.example", "company_domain_unproven"),
    ],
)
def test_domain_rejection_has_fixed_typed_reason(field, value, reason):
    from domains.prospecting.public_evidence import (
        ProjectionRejected,
        project_candidate,
    )

    args = dict(  # noqa: C408 -- typed关键词测试夹具
        source_id="company_websites",
        source_url="https://acme.example",
        text="Acme\nAddress: Kenya\ntricycle",
        content_hash="a" * 64,
        artifact_id="art_public",
        observed_at=datetime.now(UTC),
        name="Acme",
        country="KE",
        product_tags=("tricycle",),
        public_emails=(),
    )
    args[field] = value
    with pytest.raises(ProjectionRejected) as error:
        project_candidate(**args)
    assert error.value.reason == reason


async def test_collector_reports_mixed_rejections_and_valid_candidate():
    from types import SimpleNamespace

    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from connectors.web_search.client import PageSnapshot
    from shared.schemas.identifiers import new_id
    from tests.unit.test_inventory_acquisition_driver import JOB

    async def search(*args, **kwargs):
        return SimpleNamespace(
            results=(
                SimpleNamespace(description="", url="https://acme.example", title="Acme"),
                SimpleNamespace(description="", url="https://unread.example", title="Unread"),
            )
        )

    async def read(*args):
        return PageSnapshot(
            "Acme\nAddress: Kenya\ntricycles",
            "https://acme.example",
            datetime.now(UTC),
            "a" * 64,
            new_id("art"),
        )

    async def factory(*args):
        return SimpleNamespace(
            searcher=SimpleNamespace(search=search),
            page_reader=SimpleNamespace(read_page=read),
        )

    async def generate(job, source, session, payload):
        await session.call("source_search", {"source_id": source, "query": "company"})
        await session.call("read_public_page", {"page_id": "page_1"})
        return {
            "candidates": [
                None,
                {"page_id": []},
                {"page_id": "unknown"},
                {"page_id": "page_2"},
                {"page_id": "page_1", "name": "Acme", "country": "XX"},
                {"page_id": "page_1", "name": "Acme", "country": "KE"},
            ]
        }

    async def guard():
        pass

    result = await CodexInventoryCollector(factory, generate).collect(
        JOB, "company_websites", guard
    )
    assert len(result.candidates) == 1
    assert result.summary.raw_count == 6 and result.summary.projected_count == 1
    assert result.summary.rejected.model_dump(exclude_defaults=True) == {
        "item_not_object": 1,
        "item_fields_invalid": 1,
        "page_unknown": 1,
        "page_not_read": 1,
        "country_unsupported": 1,
    }
    assert result.summary.official_read_count == 1


def test_diagnostic_version_bool_is_not_an_integer_version():
    from domains.prospecting.projection_diagnostic import (
        ProjectionSummary,
        SourceDiagnostic,
    )

    with pytest.raises(ValueError):
        SourceDiagnostic(
            version=True, projection=ProjectionSummary(input_shape="valid", raw_count=0)
        )


async def test_shape_failure_persists_on_source_attempt_without_model_text():
    from apps.scheduler_worker.inventory_acquisition import InventoryAcquisitionDriver
    from domains.prospecting.projection_diagnostic import (
        ProjectionDiagnosticError,
        ProjectionSummary,
    )
    from tests.unit.test_inventory_acquisition_driver import (
        NOW,
        Authority,
        Backend,
        Catalog,
        Sources,
    )

    class Collector:
        async def collect(self, *args, **kwargs):
            raise ProjectionDiagnosticError(
                ProjectionSummary(input_shape="invalid_top_keys")
            )

    backend = Backend()
    await InventoryAcquisitionDriver(
        tenant_id="tenant",
        backend=backend,
        authority=Authority(),
        catalog=Catalog(),
        sources=Sources(),
        collector=Collector(),
        now=lambda: NOW,
    ).scan()
    assert backend.last_diagnostic.projection.input_shape == "invalid_top_keys"
    assert backend.last_diagnostic.save is None
    assert backend.calls[-1] == "collector_failed"
