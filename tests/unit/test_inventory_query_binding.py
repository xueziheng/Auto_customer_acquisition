"""必须检查实际派发检索词，不把 prompt 或 country 参数当作已绑定。"""

from types import SimpleNamespace

import pytest

from domains.prospecting.inventory import InventoryQuery
from tests.unit.test_inventory_acquisition_driver import JOB


@pytest.mark.parametrize(
    "country,region,tag,anchor",
    [
        ("NG", "africa", "三轮车", "tricycle Nigeria"),
        ("ZA", "africa", "电动三轮车", "electric tricycle South Africa"),
        (None, "africa", "三轮车", "tricycle Africa"),
        (None, None, "未知中文产品", "未知中文产品"),
        ("ZZ", None, "bolts", "bolts ZZ"),
    ],
)
async def test_actual_search_query_binds_task_anchors(country, region, tag, anchor):
    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector

    actual = []

    async def search(tenant, run, query, *args, **kwargs):
        actual.append(query)
        return SimpleNamespace(results=())

    async def factory(*args):
        return SimpleNamespace(searcher=SimpleNamespace(search=search))

    async def generate(job, source, session, payload):
        await session.call(
            "source_search", {"source_id": source, "query": "Acme products"}
        )
        return {"candidates": []}

    async def guard():
        pass

    query = InventoryQuery(country=country, region=region, product_tags=(tag,))
    job = JOB.model_copy(update={"query": query})
    await CodexInventoryCollector(factory, generate).collect(
        job, "company_websites", guard
    )
    assert len(actual) == 1
    assert actual[0].startswith(anchor + " official company website contact")
    assert "Acme products" in actual[0]
    assert job.query == query


def compose(query, supplement, strategy="official company website contact"):
    from apps.scheduler_worker.inventory_query import compose_inventory_query

    return compose_inventory_query(query, strategy, supplement)


def test_untrusted_operators_cannot_override_fixed_anchors_or_trusted_linkedin():
    query = InventoryQuery(country="NG", product_tags=("三轮车",))
    result = compose(
        query,
        '-Nigeria site:evil.example "China" OR NOT Kenya (Acme) +global |France',
        "site:linkedin.com/company",
    )
    assert result.startswith("tricycle Nigeria site:linkedin.com/company")
    assert "site:evil" not in result and "-Nigeria" not in result
    assert all(operator not in result.split() for operator in ("OR", "NOT", "AND"))
    assert '"' not in result and "|" not in result and "(" not in result
    assert "Acme" in result


@pytest.mark.parametrize(
    "supplement",
    [
        "x" * 400,
        "x" * 401,
        " ".join(f"w{i}" for i in range(50)),
        " ".join(f"w{i}" for i in range(51)),
    ],
)
def test_supplement_is_bounded_at_whole_words_without_cutting_anchors(supplement):
    query = InventoryQuery(country="ZA", product_tags=("电动三轮车",))
    result = compose(query, supplement)
    assert result.startswith(
        "electric tricycle South Africa official company website contact"
    )
    assert len(result) <= 400 and len(result.split()) <= 50
    if supplement.startswith("x"):
        assert "x" not in result


def test_exact_character_and_word_boundaries_and_multitag_input_remain_unchanged():
    query = InventoryQuery(product_tags=("未知词", "三轮车"))
    original = query.model_dump()
    fixed = compose(query, "")
    left = 400 - len(fixed) - 1
    assert len(compose(query, "x" * left)) == 400
    assert compose(query, "x" * (left + 1)) == fixed
    result = compose(query, " ".join(f"w{i}" for i in range(51)))
    assert len(result.split()) == 50
    assert query.model_dump() == original
    assert fixed.startswith(
        "tricycle "
    )  # 沿规范排序后首项，不声称用户优先级或全部覆盖。


@pytest.mark.parametrize(
    "supplement", ["bad\nquery", "bad\x00query", "bad\x7fquery", "bad\u200bquery"]
)
def test_control_characters_are_rejected_without_echo(supplement):
    with pytest.raises(ValueError) as error:
        compose(InventoryQuery(product_tags=("三轮车",)), supplement)
    assert str(error.value) == "采集检索条件无效"


def test_fixed_words_over_limit_and_contradictory_region_are_rejected():
    query = InventoryQuery(product_tags=("a",))
    for strategy in ("s" * 400, " ".join("s" for _ in range(50))):
        with pytest.raises(ValueError):
            compose(query, "", strategy)
    with pytest.raises(ValueError):
        compose(
            InventoryQuery(region="africa", country="FR", product_tags=("三轮车",)), ""
        )


def test_invalid_scope_is_rejected_before_job_or_search_dispatch():
    with pytest.raises(ValueError, match="国家与目标地区矛盾"):
        InventoryQuery(region="africa", country="FR", product_tags=("三轮车",))


def test_whitespace_unicode_and_only_first_existing_alias_are_preserved():
    query = InventoryQuery(country="NG", product_tags=("三轮车",))
    result = compose(query, "Acme\u00a0\u3000Industries tricycle Nigeria Acme")
    assert result == "tricycle Nigeria official company website contact Acme Industries"
    assert "three wheeler" not in result and "tuk tuk" not in result
    assert (
        compose(InventoryQuery(product_tags=("未知中文品名",)), "")
        == "未知中文品名 official company website contact"
    )


async def test_search_anchor_and_snippet_do_not_prove_page_country():
    from datetime import UTC, datetime

    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from connectors.web_search.client import PageSnapshot
    from shared.schemas.identifiers import new_id

    actual = []

    async def search(tenant, run, query, *args, **kwargs):
        actual.append(query)
        return SimpleNamespace(
            results=(
                SimpleNamespace(
                    url="https://acme.example",
                    title="Acme",
                    description="Acme Nigeria tricycle",
                ),
            )
        )

    async def read(*args):
        return PageSnapshot(
            "Acme\nFrance\ntricycle",
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
        found = await session.call(
            "source_search", {"source_id": source, "query": "Acme"}
        )
        await session.call(
            "read_public_page", {"page_id": found["results"][0]["page_id"]}
        )
        return {"candidates": [{"page_id": "page_1", "name": "Acme", "country": "NG"}]}

    async def guard():
        pass

    job = JOB.model_copy(
        update={"query": InventoryQuery(country="NG", product_tags=("三轮车",))}
    )
    result = await CodexInventoryCollector(factory, generate).collect(
        job, "company_websites", guard
    )
    assert "tricycle Nigeria" in actual[0]
    assert result.candidates == ()
    assert result.summary.rejected.country_address_unproven == 1
