"""受信宿主一次性SYSTEM来源诊断；只经原Gateway，最多6供应商HTTP，不读取页面。"""

import asyncio
import hashlib
import http.client
import json
import logging
import os
import ssl
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path


class SupplierRequestBudget:
    """全诊断固定六次HTTP上限，派发前消费，失败不退款。"""

    def __init__(self) -> None:
        self.count = 0

    def take(self) -> None:
        if self.count >= 6:
            raise ValueError("http_cap")
        self.count += 1


def free_grant_eligible(balance):
    """所有币种都不能有充值；只接受一致、非负、有限的官方金额字符串。"""
    rows = balance.get("balance_infos")
    if (
        balance.get("is_available") is not True
        or not isinstance(rows, list)
        or not rows
    ):
        return False
    eligible = False
    seen = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("currency") not in {"CNY", "USD"}
            or row["currency"] in seen
        ):
            return False
        seen.add(row["currency"])
        values = []
        for field in ("total_balance", "granted_balance", "topped_up_balance"):
            raw = row.get(field)
            if not isinstance(raw, str):
                return False
            try:
                value = Decimal(raw)
            except InvalidOperation:
                return False
            if not value.is_finite() or value < 0:
                return False
            values.append(value)
        total, granted, topped = values
        if total != granted + topped or topped != 0:
            return False
        if row["currency"] == "CNY" and granted >= Decimal("0.005"):
            eligible = True
    return eligible


async def main(
    profile_path,
    model_path,
    *,
    journal_path=Path("/tmp/tradeos-task7-source-smoke-20261022.json"),
):

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from apps.scheduler_worker.free_web_discovery import build_free_search_reader
    from connectors.tavily.transport import TavilySearchApiTransport
    from connectors.web_search.transport import SafePublicPageHttpTransport
    from infra.db.session import create_engine_from
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
    from infra.model_api import ModelKeyFileResolver
    from infra.model_catalog import FileModelCatalog
    from infra.pilot.config import PilotConfig
    from infra.standalone.settings import load_model_settings
    from shared.schemas.identifiers import RunId, TenantId, UserId, new_id
    from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.manifest import (
        CostClass,
        IdempotencyRequirement,
        RiskLevel,
        ToolManifest,
        ToolRegistry,
    )
    from tool_gateway.pipeline import (
        CheckRejection,
        PreparedToolCall,
        ToolCallContext,
        ToolGateway,
    )

    profile = PilotConfig.read(Path(profile_path))
    settings = load_model_settings(Path(model_path))
    tenant = TenantId(profile.tenant_id)
    run = RunId(new_id("run"))
    user = UserId("system_source_smoke")
    engine = create_engine_from(profile.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    env = profile.runtime_environment()
    fingerprint = HmacFingerprintProvider(
        env["TOOL_CALL_FINGERPRINT_KEY_VERSION"],
        profile.resolve(env["TOOL_CALL_FINGERPRINT_KEY_REF"]).encode(),
    )
    # 固定一次性任务journal先落盘；未知/中断后不允许新run重新派发。
    fd = os.open(
        journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as journal:
        json.dump({"status": "started", "run_id": str(run), "http_cap": 6}, journal)
        journal.flush()
        os.fsync(journal.fileno())
    budget = SupplierRequestBudget()
    results = {}
    used = set()
    tavily_attempted = {"usage": 0, "search": 0}
    take = budget.take

    def request(host, method, path, body=None, key=None):
        take()
        connection = http.client.HTTPSConnection(
            host, 443, timeout=15, context=ssl.create_default_context()
        )
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            }
            if key is not None:
                headers["Authorization"] = "Bearer " + key
            connection.request(
                method,
                path,
                body=None if body is None else json.dumps(body).encode(),
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read(1048577)
            if response.status != 200 or len(raw) > 1048576:
                raise ValueError("http_failed")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError("shape")
            return value
        finally:
            connection.close()

    class CountedTavily(TavilySearchApiTransport):
        async def usage(self, **kwargs):
            take()
            tavily_attempted["usage"] += 1
            return await super().usage(**kwargs)

        async def search(self, *args, **kwargs):
            take()
            tavily_attempted["search"] += 1
            return await super().search(*args, **kwargs)

    async def tavily():
        if (
            settings.research is None
            or not settings.research.exclusive_account_confirmed
        ):
            return {"status": "not_run", "reason": "exclusive_free_config_missing"}
        free = build_free_search_reader(
            factory=sessions,
            tenant_id=tenant,
            search_transport=CountedTavily(timeout_seconds=15),
            page_transport=SafePublicPageHttpTransport(timeout_seconds=10),
            secret_resolver=profile,
            secret_ref=settings.research.secret_ref,
            now=lambda: datetime.now(UTC),
        )
        # 原FreeSearchReader自行刷新一次usage，沿原免费资格/账户槽/持久reserve后才Search。
        reader = free.for_run(
            tenant,
            run,
            hashlib.sha256((str(run) + ":tavily").encode()).hexdigest(),
            fingerprint_version=fingerprint.key_version,
        )
        try:
            found = await reader.search(tenant, "steel distributor Kenya", "KE", 1)
        except Exception:
            state = await free.quota.run_state(run)
            results["tavily"] = {
                "status": "unknown" if tavily_attempted["search"] else "not_run",
                "reason": state.stop_reason.value
                if state and state.stop_reason
                else "qualification_failed",
                "attempted": dict(tavily_attempted),
            }
            raise
        return {
            "status": "sample",
            "query_count": 1,
            "result_urls": [r.url for r in found],
            "free_qualification": "original_free_quota_gate",
            "workflow": "source_sample_only",
        }

    async def deepseek():
        if (
            settings.model_catalog_directory is None
            or settings.api_control_tenant is None
        ):
            return {"status": "not_run", "reason": "platform_catalog_missing"}
        directory = settings.model_catalog_directory
        if (
            not all(
                p.is_dir()
                for p in (
                    directory,
                    directory / "catalog-lock",
                )
            )
            or not (directory / "catalog.json").is_file()
        ):
            return {"status": "not_run", "reason": "existing_catalog_paths_missing"}
        catalog = FileModelCatalog(directory, TenantId(settings.api_control_tenant))
        binding = catalog.bindings(TenantId(settings.api_control_tenant)).get(
            str(tenant)
        )
        if not binding:
            return {"status": "not_run", "reason": "official_binding_missing"}
        entry_dir = (
            directory
            if binding["model_id"] == "legacy"
            else directory / "connections" / binding["model_id"]
        )
        if not entry_dir.is_dir() or not (entry_dir / "state.json").is_file():
            return {"status": "not_run", "reason": "existing_connection_paths_missing"}
        view = catalog.get(TenantId(settings.api_control_tenant), binding["model_id"])
        if (
            not view.configured
            or view.test_status != "passed"
            or view.provider != "deepseek"
            or view.base_url
            not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
            or view.configuration_version != binding["version"]
        ):
            return {"status": "not_run", "reason": "official_current_version_missing"}
        resolver = ModelKeyFileResolver(
            catalog.model_store(binding["model_id"]).key_path(binding["version"]),
            settings.api_control_tenant,
            binding["version"],
            "MODEL_API_KEY",
        )
        key = resolver.resolve("MODEL_API_KEY")
        balance = await asyncio.to_thread(
            request, "api.deepseek.com", "GET", "/user/balance", None, key
        )
        if not free_grant_eligible(balance):
            return {
                "status": "metadata_only",
                "reason": "free_grant_not_proven",
                "query_count": 0,
            }
        found = await asyncio.to_thread(
            request,
            "api.deepseek.com",
            "POST",
            "/v1/tools/search",
            {"query": "steel distributor Kenya", "count": 1, "need_content": False},
            key,
        )
        entries = found.get("results")
        if not isinstance(entries, list):
            raise TypeError("shape")
        urls = [
            x["url"]
            for x in entries[:1]
            if isinstance(x, dict)
            and isinstance(x.get("url"), str)
            and x["url"].startswith("https://")
        ]
        return {
            "status": "sample",
            "query_count": 1,
            "result_urls": urls,
            "free_qualification": "fresh_CNY_grant_all_currencies_no_topup",
            "workflow": "source_sample_only",
        }

    async def ted():
        found = await asyncio.to_thread(
            request,
            "api.ted.europa.eu",
            "POST",
            "/v3/notices/search",
            {
                "query": "publication-date >= 20261001",
                "fields": ["publication-number", "notice-title"],
                "page": 1,
                "limit": 1,
                "paginationMode": "PAGE_NUMBER",
            },
        )
        notices = found.get("notices")
        if not isinstance(notices, list):
            raise TypeError("shape")
        urls = []

        def collect(value):
            if (
                isinstance(value, str)
                and value.startswith("https://ted.europa.eu/")
                and len(urls) < 3
            ):
                urls.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        for notice in notices[:1]:
            collect(notice)
        return {
            "status": "sample",
            "query_count": 1,
            "result_count": len(notices[:1]),
            "result_urls": urls,
            "free_qualification": "official_no_account_public_API",
            "workflow": "source_sample_only",
        }

    class Gate:
        def __init__(self, name):
            self.name = name

        async def check(self, ctx, state):
            if (
                (ctx.tenant_id, ctx.run_id, ctx.user_id) != (tenant, run, user)
                or ctx.params
                or ctx.tool_id
                not in {
                    "diagnostic.source.tavily",
                    "diagnostic.source.deepseek",
                    "diagnostic.source.ted",
                }
            ):
                return CheckRejection(self.name, "diagnostic_scope", "诊断范围不匹配")
            return None

    class Handler:
        def __init__(self, name, call):
            self.name, self.call = name, call

        async def prepare(self, ctx, preflight):
            digest, version = fingerprint.fingerprint(
                (str(tenant).encode(), str(run).encode(), self.name.encode())
            )
            return PreparedToolCall(digest, version, {}, self.name)

        async def execute(self, current, prepared):
            if current != tenant or self.name in used:
                raise ValueError("already_dispatched")
            used.add(self.name)
            before = budget.count
            try:
                results[self.name] = await self.call()
            except Exception:  # noqa: BLE001 -- 诊断外部异常只记录固定分类。
                results.setdefault(
                    self.name,
                    {"status": "unknown", "reason": "safe_diagnostic_failure"},
                )
                results[self.name]["supplier_http_requests"] = budget.count - before
                raise ToolGatewayError(
                    ToolErrorCategory.RECONCILIATION_REQUIRED
                    if results[self.name]["status"] == "unknown"
                    else ToolErrorCategory.PROVIDER_PERMANENT
                ) from None
            results[self.name]["supplier_http_requests"] = budget.count - before
            return {"provider_ref": "source-diagnostic-" + self.name}

    registry = ToolRegistry()
    for name, call in [("tavily", tavily), ("deepseek", deepseek), ("ted", ted)]:
        registry.register(
            ToolManifest(
                tool_id="diagnostic.source." + name,
                version="v1",
                description="一次有界免费来源诊断",
                risk_level=RiskLevel.LOW,
                cost_class=CostClass.FREE,
                requires_approval=False,
                idempotency=IdempotencyRequirement.NONE,
                required_permissions=("diagnostic:source",),
                checks=("tenant", "permission", "rate_limit"),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"provider_ref": {"type": "string"}},
                    "required": ("provider_ref",),
                    "additionalProperties": False,
                },
                redact_fields=(),
            ),
            Handler(name, call),
        )
    gateway = ToolGateway(
        registry,
        {name: Gate(name) for name in ("tenant", "permission", "rate_limit")},
        lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t),
        lease_duration=timedelta(minutes=2),
        lease_owner="source-smoke",
        now=lambda: datetime.now(UTC),
        id_factory=new_id,
    )
    try:
        for name in ("tavily", "deepseek", "ted"):
            result = await gateway.invoke(
                ToolCallContext(
                    tenant, user, "diagnostic.source." + name, {}, run_id=run
                )
            )
            if name not in results:
                results[name] = {
                    "status": "not_run",
                    "reason": "gateway_rejected",
                    "supplier_http_requests": 0,
                }
            results[name]["gateway_status"] = result.status.value
        print(
            json.dumps(
                {
                    "diagnostic_scope": "system_source_smoke",
                    "supplier_http_total": budget.count,
                    "http_cap": 6,
                    "results": results,
                    "page_requests": 0,
                    "model_requests": 0,
                    "business_writes": 0,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2]))
    except Exception:  # noqa: BLE001 -- CLI不能输出私有配置或供应商原文。
        print(
            json.dumps(
                {
                    "status": "unknown"
                    if Path("/tmp/tradeos-task7-source-smoke-20261022.json").exists()
                    else "not_run",
                    "reason": "trusted_diagnostic_setup_failed",
                }
            )
        )
        raise SystemExit(2)
