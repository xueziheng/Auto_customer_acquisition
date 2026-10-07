"""真实 PostgreSQL 角色、会话认证和企业 HTTP 容器的联合验收。

仅使用独立隔离测试库的合成员工、账号和产品；实际认证、员工/产品领域服务、
SQL 仓储、RLS 探针与 HTTP 中间件均不替换。不调用模型、邮件或外部服务。
其余未访问业务依赖沿用路由测试模板，不代表完整业务 runtime 已验收。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.authentication import AuthenticationCookieSettings
from apps.api.enterprise_container import (
    EnterpriseAppBinding,
    create_enterprise_container,
)
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.composition_support.employee_readers import employee_service_scope
from domains.employees.permissions import Phase1EmployeeAuthorizer, StandardAuditLogger
from domains.products.permissions import Phase2ProductAuthorizer
from domains.products.service_impl import ProductServiceImpl
from infra.authentication.service import PostgresAuthentication
from infra.db.products_uow import SqlAlchemyProductsUnitOfWork
from infra.db.tables import (
    AuthAccountRow,
    AuthRateLimitRow,
    AuthSessionRow,
    EmployeeRow,
    ProductRow,
)
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from tests.integration.test_tenant_row_security import (
    IsolationDatabase,
    isolated_database,  # noqa: F401 -- 显式导入共享的真实隔离库 fixture
    prepared_isolation_url,  # noqa: F401 -- isolated_database 的 session 级依赖
)
from tests.unit.test_crm_router import _app

_ORIGIN = "http://127.0.0.1:18761"
_TRUSTED = {"Origin": _ORIGIN, "X-TradeOS-Request": "1"}
_USERNAME = "synthetic-enterprise-member"


def _path(key: str, suffix: str) -> str:
    return "/api/enterprises/" + key + suffix


def _child(engine, tenant: str, key: str):
    """真实每企业连接池、领域授权与认证；无探针替身或共享认证实例。"""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = TenantId(tenant)
    authorizer = Phase1EmployeeAuthorizer(tenant_id)
    employees = partial(
        employee_service_scope,
        factory,
        now=lambda: datetime.now(UTC),
        authorizer=authorizer,
        audit=StandardAuditLogger(),
    )
    products = ProductServiceImpl(
        lambda requested_tenant: SqlAlchemyProductsUnitOfWork(factory, requested_tenant),
        Phase2ProductAuthorizer(tenant_id),
    )
    dependencies = replace(
        _app()[0].state.dependencies,
        employees=employees,
        employee_authorizer=authorizer,
        products=products,
    )
    authentication = PostgresAuthentication(factory, tenant_id)
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=False, retry_after_seconds=30),
        dependencies=dependencies,
        authentication=authentication,
        authentication_origin=_ORIGIN,
        authentication_cookie=AuthenticationCookieSettings(
            "tradeos_session_" + key, _path(key, "")
        ),
    )
    app.state.runtime_engine = engine
    return app, authentication, factory


async def test_real_postgres_authentication_and_products_remain_enterprise_scoped(
    isolated_database: IsolationDatabase,  # noqa: F811 -- pytest 按同名导入 fixture 注入
) -> None:
    db = isolated_database
    password = SecretStr("synthetic-enterprise-isolation-password-only")
    shared_product = str(new_id("prd"))
    exclusive_products = (str(new_id("prd")), str(new_id("prd")))
    bindings = []
    try:
        # 顺序创建与登录，避免小型验收主机同时执行两个真实 scrypt。
        for index, (engine, tenant, employee) in enumerate(
            zip(db.apps, db.tenants, db.employees, strict=True)
        ):
            key = ("a", "b")[index]
            app, auth, factory = _child(engine, tenant, key)
            async with factory.begin() as session:
                await session.execute(
                    update(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == tenant,
                        EmployeeRow.employee_id == employee,
                    )
                    .values(role="boss", name="合成员工-" + key)
                )
                for product in (shared_product, exclusive_products[index]):
                    session.add(
                        ProductRow(
                            tenant_id=tenant,
                            product_id=product,
                            pool="formal",
                            candidate_status=None,
                            name_zh="同名合成产品",
                            name_en="Same synthetic product",
                            category="synthetic",
                            normalized_category="synthetic",
                            spec_summary="enterprise-" + key + "-private-specification",
                            sellable_markets=[],
                            customizable=False,
                            selling_points=[],
                            known_issues=[],
                            created_at=datetime.now(UTC),
                        )
                    )
            await auth.create_account(_USERNAME, password, EmployeeId(employee))
            bindings.append(EnterpriseAppBinding(key, TenantId(tenant), app))

        # 不注入 isolation_probe；真实角色/策略/权限检查必须通过才能进入 lifespan。
        root = create_enterprise_container(bindings)
        async with (
            root.router.lifespan_context(root),
            AsyncClient(transport=ASGITransport(root), base_url=_ORIGIN) as client,
        ):
            logins = {}
            for index, key in enumerate(("a", "b")):
                response = await client.post(
                    _path(key, "/auth/login"),
                    headers=_TRUSTED,
                    json={"username": _USERNAME, "password": password.get_secret_value()},
                )
                assert response.status_code == 200, "真实企业账号登录失败"
                logins[key] = response
                matched = (
                    response.json()["employee"]["employee_id"] == db.employees[index]
                    and response.json()["employee"]["tenant_id"] == db.tenants[index]
                )
                assert matched, "登录会话映射到错误企业员工"
                cookie_scoped = (
                    "Path=" + _path(key, "") + ";"
                    in response.headers.get("set-cookie", "")
                )
                assert cookie_scoped, "企业会话 cookie 路径不正确"

            for index, key in enumerate(("a", "b")):
                team = await client.get(_path(key, "/team/employees"))
                assert team.status_code == 200
                assert [row["employee_id"] for row in team.json()] == [db.employees[index]]
                assert {row["tenant_id"] for row in team.json()} == {db.tenants[index]}
                products = await client.get(_path(key, "/products"))
                assert products.status_code == 200
                assert {row["product_id"] for row in products.json()} == {
                    shared_product, exclusive_products[index],
                }
                assert {row["name_en"] for row in products.json()} == {"Same synthetic product"}
                assert {row["spec_summary"] for row in products.json()} == {
                    "enterprise-" + key + "-private-specification"
                }
                wrong_product = await client.get(
                    _path(key, "/products/" + exclusive_products[1 - index] + "/sales")
                )
                assert wrong_product.status_code == 404

            # 原 cookie 名及伪造为另一企业 cookie 名都不能授权另一企业路径。
            for source, target in (("a", "b"), ("b", "a")):
                token = client.cookies.get("tradeos_session_" + source)
                for cookie_key in (source, target):
                    denied = await client.get(
                        _path(target, "/team/employees"),
                        headers={"Cookie": "tradeos_session_" + cookie_key + "=" + token},
                    )
                    assert denied.status_code == 401, "跨企业 cookie 被接受"
                    assert denied.headers.get("cache-control") == "no-store"

            wrong_csrf = await client.post(
                _path("b", "/auth/logout"),
                headers={**_TRUSTED, "X-CSRF-Token": logins["a"].json()["csrf_token"]},
            )
            assert wrong_csrf.status_code == 401
            missing_csrf = await client.post(_path("b", "/auth/logout"), headers=_TRUSTED)
            assert missing_csrf.status_code == 403
            assert (await client.get(_path("b", "/auth/session"))).status_code == 200

            keys = ["a", "b"] * 6
            responses = await asyncio.gather(
                *(client.get(_path(key, "/products")) for key in keys)
            )
            for key, response in zip(keys, responses, strict=True):
                assert response.status_code == 200
                assert {row["spec_summary"] for row in response.json()} == {
                    "enterprise-" + key + "-private-specification"
                }

            logged_out = await client.post(
                _path("a", "/auth/logout"),
                headers={**_TRUSTED, "X-CSRF-Token": logins["a"].json()["csrf_token"]},
            )
            assert logged_out.status_code == 204
            assert (await client.get(_path("a", "/auth/session"))).status_code == 401
            assert (await client.get(_path("b", "/auth/session"))).status_code == 200
    finally:
        # 仅清理本次 fixture 随机企业；原 fixture 随后回收员工、角色和连接池。
        try:
            factory = async_sessionmaker(db.admin, expire_on_commit=False)
            async with factory.begin() as session:
                for tenant in db.tenants:
                    for model in (AuthSessionRow, AuthAccountRow, AuthRateLimitRow, ProductRow):
                        await session.execute(delete(model).where(model.tenant_id == tenant))
        except Exception:  # noqa: BLE001 -- 不回显数据库或会话材料
            raise AssertionError("企业 HTTP 联合验收资料清理失败") from None
