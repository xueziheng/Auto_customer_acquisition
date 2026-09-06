"""本owner研究合成外部端口；不联网、不生成领域对象或审批事实。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from connectors.tavily.transport import TavilyHttpResponse
from connectors.web_search.transport import PublicPageResponse

from .config import ControlledError

PAGES = {
    lane: (
        f"https://controlled-{lane}.example.com/about",
        (
            f"We are Controlled {lane.title()}, a furniture hardware {lane}. "
            "We are based in KE. We are expanding our furniture hardware product line."
        ),
    )
    for lane in ("importer", "distributor", "ecommerce")
}


class ControlledResearchPages:
    """仅返回精确合成URL；无DNS/HTTP fallback。"""

    async def validate_url(self, url: str) -> str:
        if url not in {item[0] for item in PAGES.values()}:
            raise ControlledError("controlled_research_input_required")
        return url

    async def fetch(self, url: str) -> PublicPageResponse:
        await self.validate_url(url)
        body = next(text for address, text in PAGES.values() if address == url)
        return PublicPageResponse(
            url, ("<html><body><p>" + body + "</p></body></html>").encode()
        )


class ControlledResearchSearch:
    """合成免费三查询账户；真实预算由原PostgreSQL quota保守累计。"""

    async def usage(self, *, api_key: str) -> TavilyHttpResponse:
        del api_key
        return TavilyHttpResponse(
            200,
            {
                "account": {
                    "current_plan": "Researcher",
                    "plan_limit": 3,
                    "plan_usage": 0,
                    "paygo_usage": 0,
                    "paygo_limit": 0,
                }
            },
        )

    async def search(
        self, query: str, country: str, limit: int, *, api_key: str
    ) -> TavilyHttpResponse:
        del api_key
        lane = next(
            (lane for lane in PAGES if query == "Kenya furniture hardware " + lane),
            None,
        )
        if lane is None or country != "KE" or type(limit) is not int or limit != 1:
            raise ControlledError("controlled_research_input_required")
        url, body = PAGES[lane]
        return TavilyHttpResponse(
            200,
            {
                "results": [
                    {"title": "Controlled " + lane.title(), "url": url, "content": body}
                ]
            },
        )


class ControlledResearchModel:
    """只响应原能力裁剪后的具名合成页面，所有业务证据仍经原Agent验证。"""

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        del system_prompt
        expected = {
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
        if (
            model != "controlled-research-v1"
            or dict(payload) != expected
            or max_output_tokens != 3000
        ):
            raise ControlledError("controlled_research_input_required")
        return json.dumps(
            {
                "signals": [
                    {
                        "signal_type": "product_line_expansion",
                        "source_page_index": index,
                        "source_excerpt": body,
                        "possible_need": "furniture hardware",
                        "evidence_level": "public_company_event",
                    }
                    for index, (_, body) in enumerate(PAGES.values())
                ],
                "hypotheses": [
                    {
                        "account_name_signal_index": index,
                        "country_signal_index": index,
                        "country": "KE",
                        "category": "furniture hardware",
                        "signal_indexes": [index],
                        "reasoning": "公司自述扩充家具五金品类，可能需要相关配件，值得验证。",
                    }
                    for index in range(3)
                ],
            }
        )
