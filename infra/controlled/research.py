"""本owner研究合成外部端口；不联网、不生成领域对象或审批事实。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from uuid import uuid4

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


class ControlledResearchCalls:
    """同owner私有邮件库只记逐次操作名，不保存输入、凭证或模型正文。"""

    def __init__(self, path: Path, *, tenant_id: str) -> None:
        self._path, self._tenant = path, tenant_id

    def record(self, operation: str) -> None:
        if operation not in {"research.usage", "research.search", "research.page", "research.model"}:
            raise ControlledError("controlled_research_operation_required")
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS controlled_research_calls (tenant_id TEXT NOT NULL, call_id TEXT PRIMARY KEY, operation TEXT NOT NULL)")
            db.execute("INSERT INTO controlled_research_calls VALUES (?, ?, ?)", (self._tenant, uuid4().hex, operation))

    def list_calls(self) -> tuple[str, ...]:
        if not self._path.exists():
            return ()
        with closing(sqlite3.connect(self._path)) as db:
            if not db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='controlled_research_calls'").fetchone()[0]:
                return ()
            return tuple(row[0] for row in db.execute("SELECT operation FROM controlled_research_calls WHERE tenant_id=? ORDER BY rowid", (self._tenant,)))


class ControlledResearchPages:
    """仅返回精确合成URL；无DNS/HTTP fallback。"""

    def __init__(self, calls: ControlledResearchCalls | None = None) -> None:
        self._calls = calls

    async def validate_url(self, url: str) -> str:
        if url not in {item[0] for item in PAGES.values()}:
            raise ControlledError("controlled_research_input_required")
        return url

    async def fetch(self, url: str) -> PublicPageResponse:
        if self._calls is not None:
            self._calls.record("research.page")
        await self.validate_url(url)
        body = next(text for address, text in PAGES.values() if address == url)
        return PublicPageResponse(
            url, ("<html><body><p>" + body + "</p></body></html>").encode()
        )


class ControlledResearchSearch:
    """合成免费三查询账户；真实预算由原PostgreSQL quota保守累计。"""

    def __init__(self, calls: ControlledResearchCalls | None = None) -> None:
        self._calls = calls

    async def usage(self, *, api_key: str) -> TavilyHttpResponse:
        del api_key
        if self._calls is not None:
            self._calls.record("research.usage")
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
        if self._calls is not None:
            self._calls.record("research.search")
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

    def __init__(self, calls: ControlledResearchCalls | None = None) -> None:
        self._calls = calls

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        del system_prompt
        if self._calls is not None:
            self._calls.record("research.model")
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
