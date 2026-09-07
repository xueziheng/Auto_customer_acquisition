"""持久化本机内测的生产构建、真实会话与冷恢复浏览器验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import BrowserContext, Page, Route, async_playwright, expect
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.authentication import session_cookie_name
from apps.api.pilot_accounts import AccountCommand, run_account_command
from infra.db.session import create_engine_from
from infra.db.tables import EmployeeRow, TerritoryAssignmentRow
from infra.pilot.backup import backup_profile, restore_profile
from infra.pilot.config import PilotConfig
from infra.pilot.resources import OWNER_LABEL, PilotProfile
from scripts.run_web_pilot import start_profile
from shared.schemas.identifiers import TenantId, new_id
from tests.unit.test_pilot_profile import synthetic_policy

pytestmark = pytest.mark.e2e


@dataclass(frozen=True, repr=False)
class Credentials:
    username: str
    password: SecretStr
    name: str
    role: str
    employee_id: str


@dataclass(frozen=True)
class OwnedProfile:
    path: Path
    owner: str
    resources: dict[str, tuple[str, str, str]]


def require(condition: bool, reason: str) -> None:
    """敏感链路失败只暴露固定诊断，不展开请求、cookie或配置。"""
    if not condition:
        raise AssertionError(reason)


@pytest_asyncio.fixture
async def owned_pilot(tmp_path: Path) -> AsyncIterator[tuple[Path, list[OwnedProfile]]]:
    """只登记并删除本 fixture 创建且再次精确核验过的 owner 资源。"""
    owned: list[OwnedProfile] = []
    yield tmp_path, owned
    cleanup_errors = 0
    for record in reversed(owned):
        profile: PilotProfile | None = None
        try:
            profile = PilotProfile(record.path)
            require(profile.config.owner == record.owner, "CLEANUP_OWNER_MISMATCH")
            await asyncio.to_thread(profile.stop)
            profile.reload()
            require(profile.config.owner == record.owner, "CLEANUP_OWNER_MISMATCH")
            require(
                set(profile.config.storage) == set(record.resources),
                "CLEANUP_RESOURCE_SET_MISMATCH",
            )
            verified = []
            for kind, expected in record.resources.items():
                container = profile.verify(kind)  # exact id/image/owner/volume/mount
                identity = profile.config.storage[kind]
                require(
                    (container.id, identity.volume_name, identity.volume_created_at)
                    == expected,
                    "CLEANUP_RESOURCE_MISMATCH",
                )
                volume = profile.client.volumes.get(identity.volume_name)
                volume.reload()
                require(
                    volume.attrs["Labels"].get(OWNER_LABEL) == record.owner,
                    "CLEANUP_VOLUME_OWNER_MISMATCH",
                )
                verified.append((container, volume))
            for container, volume in verified:
                container.remove()
                volume.remove()
        except BaseException:  # noqa: BLE001 - 尽量清理其余精确登记资源
            cleanup_errors += 1
        finally:
            if profile is not None:
                profile.client.close()
    require(cleanup_errors == 0, "OWNED_RESOURCE_CLEANUP_FAILED")


def remember(profile: PilotProfile, owned: list[OwnedProfile]) -> None:
    profile.reload()
    resources = {
        kind: (
            identity.container_id,
            identity.volume_name,
            identity.volume_created_at,
        )
        for kind, identity in profile.config.storage.items()
    }
    require(set(resources) == {"database", "objects"}, "OWNED_RESOURCE_SET_INVALID")
    owned.append(OwnedProfile(profile.path, profile.config.owner, resources))


async def account(
    profile: PilotProfile,
    *,
    action: str,
    username: str,
    password: SecretStr | None = None,
    name: str | None = None,
    role: str | None = None,
    manager_id: str | None = None,
) -> None:
    profile.reload()
    engine = create_engine_from(profile.config.database_url.get_secret_value())
    try:
        await run_account_command(
            async_sessionmaker(engine, expire_on_commit=False),
            TenantId(profile.config.tenant_id),
            AccountCommand(
                action=action,  # type: ignore[arg-type]
                username=username,
                name=name,
                role=role,
                manager_id=manager_id,
            ),
            password=password,
        )
    finally:
        await engine.dispose()


async def employee_id(profile: PilotProfile, name: str) -> str:
    profile.reload()
    engine = create_engine_from(profile.config.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            values = list(
                await session.scalars(
                    select(EmployeeRow.employee_id).where(
                        EmployeeRow.tenant_id == profile.config.tenant_id,
                        EmployeeRow.name == name,
                    )
                )
            )
            require(len(values) == 1, "SYNTHETIC_EMPLOYEE_LOOKUP_FAILED")
            return values[0]
    finally:
        await engine.dispose()


async def create_credentials(
    profile: PilotProfile,
    *,
    username: str,
    name: str,
    role: str,
    manager_id: str | None = None,
) -> Credentials:
    password = SecretStr(secrets.token_urlsafe(24))
    await account(
        profile,
        action="create",
        username=username,
        password=password,
        name=name,
        role=role,
        manager_id=manager_id,
    )
    return Credentials(username, password, name, role, await employee_id(profile, name))


async def seed_business_fixture(
    profile: PilotProfile, *, sales: Credentials, manager: Credentials
) -> tuple[str, str, bytes]:
    """显式合成团队分配和对象；不表示真实需求、客户验证或市场成绩。"""
    profile.reload()
    assignment_id = new_id("ter")
    engine = create_engine_from(profile.config.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False).begin() as session:
            session.add(
                TerritoryAssignmentRow(
                    assignment_id=assignment_id,
                    tenant_id=profile.config.tenant_id,
                    employee_id=sales.employee_id,
                    priority=1,
                    effective_from=datetime.now(UTC),
                    countries=["XZ"],
                    product_categories=[],
                    need_categories=["synthetic-fixture"],
                    buyer_types=[],
                    languages=[],
                    manager_id=manager.employee_id,
                    backup_employee_id=None,
                    effective_until=None,
                )
            )
    finally:
        await engine.dispose()
    object_key = "pilot-acceptance/synthetic-fixture.bin"
    content = secrets.token_bytes(128)
    with profile.object_client() as client:
        await asyncio.to_thread(
            client.put_object,
            Bucket=profile.config.bucket,
            Key=object_key,
            Body=content,
        )
    return assignment_id, object_key, content


async def database_marker(profile: PilotProfile) -> str:
    profile.reload()
    engine = create_engine_from(profile.config.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            employees = list(
                (
                    await session.execute(
                        select(
                            EmployeeRow.employee_id,
                            EmployeeRow.name,
                            EmployeeRow.role,
                            EmployeeRow.manager_id,
                        )
                        .where(EmployeeRow.tenant_id == profile.config.tenant_id)
                        .order_by(EmployeeRow.employee_id)
                    )
                ).tuples()
            )
            territories = list(
                (
                    await session.execute(
                        select(
                            TerritoryAssignmentRow.assignment_id,
                            TerritoryAssignmentRow.employee_id,
                            TerritoryAssignmentRow.countries,
                            TerritoryAssignmentRow.need_categories,
                        )
                        .where(
                            TerritoryAssignmentRow.tenant_id
                            == profile.config.tenant_id
                        )
                        .order_by(TerritoryAssignmentRow.assignment_id)
                    )
                ).tuples()
            )
    finally:
        await engine.dispose()
    canonical = json.dumps(
        {"employees": employees, "territories": territories},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def object_marker(profile: PilotProfile, key: str) -> str:
    profile.reload()
    with profile.object_client() as client:
        response = await asyncio.to_thread(
            client.get_object, Bucket=profile.config.bucket, Key=key
        )
        body = response["Body"]
        try:
            content = await asyncio.to_thread(body.read)
        finally:
            body.close()
    require(isinstance(content, bytes), "SYNTHETIC_OBJECT_READ_FAILED")
    return hashlib.sha256(content).hexdigest()


async def sign_in(page: Page, credentials: Credentials) -> None:
    await expect(page.get_by_role("button", name="登录", exact=True)).to_be_visible()
    await page.get_by_label("账号", exact=True).fill(credentials.username)
    await page.get_by_label("密码", exact=True).fill(
        credentials.password.get_secret_value()
    )
    await page.get_by_role("button", name="登录", exact=True).click()
    await expect(page.get_by_role("button", name="退出", exact=True)).to_be_visible(
        timeout=20_000
    )


async def rejected_sign_in(page: Page, credentials: Credentials) -> None:
    await expect(page.get_by_role("button", name="登录", exact=True)).to_be_visible()
    await page.get_by_label("账号", exact=True).fill(credentials.username)
    await page.get_by_label("密码", exact=True).fill(
        credentials.password.get_secret_value()
    )
    await page.get_by_role("button", name="登录", exact=True).click()
    await expect(page.get_by_role("alert")).to_contain_text("登录失败")


async def safe_session(page: Page) -> dict[str, object]:
    """丢弃 CSRF，只返回验收需要的安全身份字段。"""
    return await page.evaluate(
        """async () => {
          const response = await fetch('/api/auth/session', {cache: 'no-store'});
          if (!response.ok) return {status: response.status};
          const value = await response.json();
          return {
            status: response.status,
            employee_id: value.employee.employee_id,
            role: value.employee.role,
            active: value.employee.is_active,
          };
        }"""
    )


async def safe_status(context: BrowserContext, url: str) -> int:
    return (await context.request.get(url, fail_on_status_code=False)).status


async def test_built_web_pilot_persists_auth_and_restores_to_new_owner(
    owned_pilot: tuple[Path, list[OwnedProfile]],
) -> None:
    """完整生命周期仅操作随机 owned profile、合成账号和合成业务资料。"""
    require(os.environ.get("TRADEOS_REQUIRE_E2E") == "1", "E2E_OPT_IN_REQUIRED")
    tmp_path, owned = owned_pilot
    policy = synthetic_policy(tmp_path / "policy.json")
    source_path = tmp_path / "pilot-source"
    PilotConfig.create(source_path, policy)
    source = PilotProfile(source_path)
    browser = None
    context = None
    target: PilotProfile | None = None
    try:
        await asyncio.to_thread(source.provision_storage)
        remember(source, owned)
        await asyncio.to_thread(source.migrate)
        source.reload()

        boss = await create_credentials(
            source,
            username="synthetic-boss",
            name="内测合成老板",
            role="boss",
        )
        manager = await create_credentials(
            source,
            username="synthetic-manager",
            name="内测合成经理",
            role="manager",
        )
        sales = await create_credentials(
            source,
            username="synthetic-sales",
            name="内测合成员工",
            role="sales",
            manager_id=manager.employee_id,
        )
        _, object_key, object_content = await seed_business_fixture(
            source, sales=sales, manager=manager
        )
        expected_database = await database_marker(source)
        expected_object = hashlib.sha256(object_content).hexdigest()
        require(
            await object_marker(source, object_key) == expected_object,
            "SOURCE_OBJECT_HASH_MISMATCH",
        )

        await asyncio.to_thread(start_profile, source.path)
        source.reload()
        status = await asyncio.to_thread(source.status)
        require(
            status["storage"] == "running" and status["applications"] == "running",
            "SOURCE_START_FAILED",
        )
        source_origin = str(status["web_url"])

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900}
            )
            browser_errors: list[str] = []
            expected_network_fault = False

            def observe(created: Page) -> None:
                created.on("pageerror", lambda _: browser_errors.append("pageerror"))
                created.on(
                    "console",
                    lambda message: browser_errors.append("console_warning_or_error")
                    if message.type in {"warning", "error"}
                    and not expected_network_fault
                    else None,
                )

            context.on("page", observe)
            page = await context.new_page()
            await page.goto(source_origin + "/team")
            await sign_in(page, boss)
            await expect(page.get_by_text(boss.name, exact=True)).to_be_visible()
            await expect(page.get_by_text(sales.name, exact=True)).to_be_visible()
            require(
                await safe_session(page)
                == {
                    "status": 200,
                    "employee_id": boss.employee_id,
                    "role": "boss",
                    "active": True,
                },
                "BOSS_CURRENT_AUTHORIZATION_MISMATCH",
            )
            require(
                await safe_status(context, source_origin + "/api/team/employees")
                == 200,
                "BOSS_TEAM_AUTHORIZATION_DENIED",
            )

            await page.reload()
            await expect(page.get_by_text(sales.name, exact=True)).to_be_visible()
            require(await safe_session(page).get("status") == 200, "REFRESH_LOST_SESSION")

            await asyncio.to_thread(source.stop)
            stopped = await asyncio.to_thread(source.status)
            require(
                stopped["storage"] == "stopped"
                and stopped["applications"] == "stopped",
                "SOURCE_STOP_FAILED",
            )
            await asyncio.to_thread(start_profile, source.path)
            source.reload()
            await page.reload()
            await expect(page.get_by_text(sales.name, exact=True)).to_be_visible(
                timeout=20_000
            )
            require(
                await database_marker(source) == expected_database,
                "RESTART_DATABASE_HASH_MISMATCH",
            )
            require(
                await object_marker(source, object_key) == expected_object,
                "RESTART_OBJECT_HASH_MISMATCH",
            )

            failed_once = False

            async def fail_first_logout(route: Route) -> None:
                nonlocal expected_network_fault, failed_once
                if route.request.method == "POST" and not failed_once:
                    failed_once = True
                    expected_network_fault = True
                    await route.abort("failed")
                else:
                    await route.continue_()

            await page.route("**/api/auth/logout", fail_first_logout)
            await page.get_by_role("button", name="退出", exact=True).click()
            await expect(page.get_by_role("alert")).to_contain_text("退出未完成")
            expected_network_fault = False
            require(
                await safe_status(context, source_origin + "/api/auth/session") == 200,
                "FAILED_LOGOUT_REVOKED_SERVER_SESSION",
            )
            await page.unroute("**/api/auth/logout", fail_first_logout)
            await page.get_by_role("button", name="重试退出", exact=True).click()
            await expect(page.get_by_role("button", name="登录", exact=True)).to_be_visible()
            require(
                await safe_status(context, source_origin + "/api/auth/session") == 401,
                "LOGOUT_RETRY_DID_NOT_REVOKE_SESSION",
            )

            second = await context.new_page()
            await second.goto(source_origin + "/team")
            await expect(second.get_by_role("button", name="登录", exact=True)).to_be_visible()
            await sign_in(page, boss)
            await expect(page.get_by_text(sales.name, exact=True)).to_be_visible()
            await expect(second.get_by_role("button", name="登录", exact=True)).to_be_visible()

            held = asyncio.Event()
            release = asyncio.Event()
            order: list[str] = []

            async def hold_logout(route: Route) -> None:
                if route.request.method != "POST":
                    await route.continue_()
                    return
                order.append("logout_headers")
                held.set()
                await release.wait()
                await route.continue_()

            def record_login(request: object) -> None:
                if getattr(request, "method", None) == "POST" and str(
                    getattr(request, "url", "")
                ).endswith("/api/auth/login"):
                    order.append("login_request")

            await page.route("**/api/auth/logout", hold_logout)
            second.on("request", record_login)
            await page.get_by_role("button", name="退出", exact=True).click()
            await asyncio.wait_for(held.wait(), timeout=10)
            await second.get_by_label("账号", exact=True).fill(sales.username)
            await second.get_by_label("密码", exact=True).fill(
                sales.password.get_secret_value()
            )
            await second.get_by_role("button", name="登录", exact=True).click()
            await asyncio.sleep(0.3)
            require(order == ["logout_headers"], "AUTHENTICATION_LOCK_ORDER_INVALID")
            release.set()
            await expect(
                second.get_by_role("button", name="退出", exact=True)
            ).to_be_visible(timeout=20_000)
            require(
                order == ["logout_headers", "login_request"],
                "AUTHENTICATION_LOCK_ORDER_INVALID",
            )
            await page.unroute("**/api/auth/logout", hold_logout)
            await expect(page.get_by_role("button", name="登录", exact=True)).to_be_visible()
            await expect(page.get_by_text(sales.name, exact=True)).to_have_count(0)
            require(
                await safe_session(second)
                == {
                    "status": 200,
                    "employee_id": sales.employee_id,
                    "role": "sales",
                    "active": True,
                },
                "SALES_CURRENT_AUTHORIZATION_MISMATCH",
            )
            require(
                await safe_status(context, source_origin + "/api/team/employees")
                == 403,
                "SALES_TEAM_AUTHORIZATION_NOT_ENFORCED",
            )

            await account(
                source, action="disable", username=sales.username
            )
            require(
                await safe_status(context, source_origin + "/api/auth/session") == 401,
                "DISABLE_DID_NOT_REVOKE_SESSION",
            )
            await second.reload()
            await rejected_sign_in(second, sales)
            await account(source, action="enable", username=sales.username)
            await sign_in(second, sales)

            replacement = Credentials(
                sales.username,
                SecretStr(secrets.token_urlsafe(24)),
                sales.name,
                sales.role,
                sales.employee_id,
            )
            await account(
                source,
                action="reset-password",
                username=sales.username,
                password=replacement.password,
            )
            require(
                await safe_status(context, source_origin + "/api/auth/session") == 401,
                "PASSWORD_RESET_DID_NOT_REVOKE_SESSION",
            )
            await second.reload()
            await rejected_sign_in(second, sales)
            await sign_in(second, replacement)
            await second.get_by_role("button", name="退出", exact=True).click()
            await expect(second.get_by_role("button", name="登录", exact=True)).to_be_visible()
            require(
                await safe_status(context, source_origin + "/api/auth/session") == 401,
                "FINAL_LOGOUT_DID_NOT_REVOKE_SESSION",
            )

            await sign_in(page, boss)
            cookie_name = session_cookie_name(source_origin)
            matches = [
                item
                for item in await context.cookies(source_origin + "/api")
                if item["name"] == cookie_name
            ]
            require(len(matches) == 1, "SOURCE_SESSION_COOKIE_MISSING")
            source_session = matches[0]["value"]

            await asyncio.to_thread(source.stop)
            backup_path = tmp_path / "pilot-backup"
            await asyncio.to_thread(backup_profile, source.path, backup_path)
            target_path = tmp_path / "pilot-restored"
            target = await asyncio.to_thread(
                restore_profile, backup_path, target_path
            )
            remember(target, owned)
            target.reload()
            source.reload()
            require(target.config.owner != source.config.owner, "RESTORE_OWNER_REUSED")
            require(
                target.config.tenant_id == source.config.tenant_id
                and target.config.bucket == source.config.bucket,
                "RESTORE_BUSINESS_BINDING_CHANGED",
            )
            require(
                target.config.api_port != source.config.api_port,
                "RESTORE_TARGET_PORT_REUSED",
            )

            await asyncio.to_thread(start_profile, source.path)
            source.reload()
            require(
                await safe_status(context, source_origin + "/api/auth/session") == 200,
                "SOURCE_SESSION_CHANGED_BY_RESTORE",
            )
            require(
                await database_marker(source) == expected_database,
                "SOURCE_DATABASE_CHANGED_BY_RESTORE",
            )
            require(
                await object_marker(source, object_key) == expected_object,
                "SOURCE_OBJECT_CHANGED_BY_RESTORE",
            )
            await asyncio.to_thread(source.stop)

            await asyncio.to_thread(start_profile, target.path)
            target.reload()
            target_origin = f"http://127.0.0.1:{target.config.api_port}"
            await context.add_cookies(
                [
                    {
                        "name": session_cookie_name(target_origin),
                        "value": source_session,
                        "domain": "127.0.0.1",
                        "path": "/api",
                        "httpOnly": True,
                        "secure": False,
                        "sameSite": "Strict",
                    }
                ]
            )
            require(
                await safe_status(context, target_origin + "/api/auth/session") == 401,
                "RESTORED_OLD_SESSION_ACCEPTED",
            )
            restored_page = await context.new_page()
            await restored_page.set_viewport_size({"width": 390, "height": 844})
            await restored_page.goto(target_origin + "/team")
            await sign_in(restored_page, boss)
            await expect(restored_page.get_by_text(sales.name, exact=True)).to_be_visible()
            require(
                await database_marker(target) == expected_database,
                "RESTORE_DATABASE_HASH_MISMATCH",
            )
            require(
                await object_marker(target, object_key) == expected_object,
                "RESTORE_OBJECT_HASH_MISMATCH",
            )
            require(
                await restored_page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                ),
                "MOBILE_HORIZONTAL_OVERFLOW",
            )
            require(
                await restored_page.locator("vite-error-overlay").count() == 0,
                "FRAMEWORK_ERROR_OVERLAY",
            )
            await restored_page.get_by_role("button", name="退出", exact=True).click()
            await expect(
                restored_page.get_by_role("button", name="登录", exact=True)
            ).to_be_visible()
            require(browser_errors == [], "BROWSER_PAGE_ERROR")
    finally:
        if context is not None:
            await context.close()
        if browser is not None:
            await browser.close()
        for profile in (target, source):
            if profile is not None:
                stop_failed = False
                try:
                    await asyncio.to_thread(profile.stop)
                except BaseException:  # noqa: BLE001 - fixture 将按精确登记再次收口
                    stop_failed = True
                profile.client.close()
                if stop_failed:
                    # owned_pilot 的精确 owner 清理仍会执行，并负责报告清理失败。
                    continue
