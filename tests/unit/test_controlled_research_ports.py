"""受控研究只允许具名合成输入，不构造公网连接。"""

import json

import pytest

from infra.controlled.config import ControlledError


async def test_research_search_page_and_model_have_exact_allowlists():
    from infra.controlled.research import (
        PAGES,
        ControlledResearchModel,
        ControlledResearchPages,
        ControlledResearchSearch,
    )

    pages = ControlledResearchPages()
    search = ControlledResearchSearch()
    assert (await search.usage(api_key="placeholder")).payload["account"][
        "plan_limit"
    ] == 3
    for lane, (url, body) in PAGES.items():
        result = await search.search(
            "Kenya furniture hardware " + lane, "KE", 1, api_key="placeholder"
        )
        assert result.payload["results"][0]["url"] == url
        assert await pages.validate_url(url) == url
        assert body.encode() in (await pages.fetch(url)).body
    for args in [
        ("unknown", "KE", 1),
        ("Kenya furniture hardware importer", "US", 1),
        ("Kenya furniture hardware importer", "KE", 2),
    ]:
        with pytest.raises(ControlledError):
            await search.search(*args, api_key="placeholder")
    for url in ["https://example.org/", next(iter(PAGES.values()))[0] + "?other=1"]:
        with pytest.raises(ControlledError):
            await pages.fetch(url)
    model = ControlledResearchModel()
    payload = {
        "pages": tuple({"text": body} for _, body in PAGES.values()),
        "target_countries": ("KE",),
        "target_categories": ("furniture hardware",),
        "excluded_countries": (),
        "excluded_categories": (),
        "max_signals": 3,
        "max_hypotheses": 3,
        "strategy_group": "controlled-research",
        "execution_mode": "research_only",
    }
    result = json.loads(
        await model.complete_json(
            model="controlled-research-v1",
            system_prompt="",
            payload=payload,
            max_output_tokens=3000,
        )
    )
    assert len(result["signals"]) == len(result["hypotheses"]) == 3
    with pytest.raises(ControlledError):
        await model.complete_json(
            model="controlled-research-v1",
            system_prompt="",
            payload={**payload, "pages": ({"text": "unregistered"},)},
            max_output_tokens=3000,
        )
