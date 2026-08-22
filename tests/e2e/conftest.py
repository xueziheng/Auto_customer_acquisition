from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from apps.api.runtime_config import Phase1RuntimeSettings
from infra.db.session import create_engine_from
from infra.db.tables import EmployeeRow, TerritoryAssignmentRow
from shared.schemas.identifiers import EmployeeId, TenantId, new_id

_CONTAINER_IMAGE = "pgvector/pgvector:pg16"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_TIME = datetime(2026, 8, 10, tzinfo=UTC)


@dataclass(frozen=True)
class E2EEmployees:
    boss: EmployeeId
    manager: EmployeeId
    sales_a: EmployeeId
    sales_b: EmployeeId


@dataclass
class ManagedProcess:
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


@dataclass(frozen=True)
class E2EStack:
    api_origin: str
    web_origin: str
    tenant_id: TenantId
    employees: E2EEmployees
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    runtime_settings: Phase1RuntimeSettings
    api_process: ManagedProcess
    vite_process: ManagedProcess


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _to_asyncpg(url: str) -> str:
    if url.startswith("postgresql+psycopg2://"):
        return url.replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://", 1
        )
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise ValueError("测试数据库连接串不是 PostgreSQL 方言")


def _minimal_process_env() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(_REPO_ROOT),
        "LANG": "C.UTF-8",
    }


def _runtime_process_env(
    database_url: str,
    tenant_id: TenantId,
    vite_origin: str,
) -> dict[str, str]:
    return {
        **_minimal_process_env(),
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": str(tenant_id),
        "TRADEOS_DEV_MODE": "true",
        "TRADEOS_CORS_ALLOWED_ORIGINS": json.dumps([vite_origin]),
        "TRADEOS_API_RETRY_AFTER_SECONDS": "2",
        "TRADEOS_HANDOFF_POLICY": json.dumps(
            {
                "sla_seconds": 30,
                "backlog_threshold": 10,
                "t1_seconds": 2,
                "t2_seconds": 2,
            }
        ),
        "TRADEOS_SCORING_POLICY": json.dumps(
            {
                "version": "e2e-v1",
                "currency": "USD",
                "value_band_boundaries": ["1000.00", "5000.00"],
                "bucket_map": {
                    "1": "low",
                    "2": "low",
                    "3": "mid",
                    "4": "mid",
                    "5": "high",
                    "6": "high",
                    "7": "high",
                },
            }
        ),
        "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
        "GMAIL_OAUTH_TOKEN_REF": "gmail-oauth-phase1",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TOOL_FINGERPRINT_KEY": "f" * 32,
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
            '{"2026-v1":"UNSUBSCRIBE_HMAC_2026"}'
        ),
        "UNSUBSCRIBE_HMAC_2026": "u" * 32,
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "S3_ENDPOINT": "http://127.0.0.1:19000",
        "S3_BUCKET_ARTIFACTS": "tradeos-test-artifacts",
        "S3_ACCESS_KEY_REF": "TEST_S3_ACCESS_KEY",
        "S3_SECRET_KEY_REF": "TEST_S3_SECRET_KEY",
        "S3_REGION": "us-east-1",
        "RAW_ARTIFACT_MAX_BYTES": "10485760",
        "GENERATED_ARTIFACT_MAX_BYTES": "1048576",
        "TEST_S3_ACCESS_KEY": "test-access-key",
        "TEST_S3_SECRET_KEY": "test-secret-key",
    }


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request_status(url: str, headers: dict[str, str] | None = None) -> int:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=1) as response:
            response.read()
            return response.status
    except HTTPError as exc:
        return exc.code


async def _wait_for_http(
    url: str,
    process: ManagedProcess,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    while loop.time() < deadline:
        returncode = process.process.poll()
        if returncode is not None:
            raise AssertionError(
                "服务进程提前退出："
                f"returncode={returncode}，stdout={process.stdout_path}，"
                f"stderr={process.stderr_path}"
            )
        try:
            status = await asyncio.to_thread(_request_status, url, headers)
        except (OSError, URLError):
            status = 0
        if status == 200:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        "服务 readiness 超时："
        f"stdout={process.stdout_path}，stderr={process.stderr_path}"
    )


async def _seed_employees_and_territories(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    employees: E2EEmployees,
) -> None:
    session = factory()
    try:
        for employee_id, name, role, manager_id in (
            (employees.boss, "E2E Boss", "boss", None),
            (employees.manager, "E2E Manager", "manager", employees.boss),
            (employees.sales_a, "E2E Sales A", "sales", employees.manager),
            (employees.sales_b, "E2E Sales B", "sales", employees.manager),
        ):
            session.add(
                EmployeeRow(
                    employee_id=str(employee_id),
                    tenant_id=str(tenant_id),
                    name=name,
                    role=role,
                    created_at=_SEED_TIME,
                    user_id=None,
                    team_id=None,
                    manager_id=str(manager_id) if manager_id is not None else None,
                    languages=[],
                    timezone="UTC",
                    is_active=True,
                    max_active_accounts=None,
                )
            )
        await session.flush()
        for employee_id, country, category in (
            (employees.sales_a, "US", "hinges"),
            (employees.sales_b, "CA", "fasteners"),
        ):
            session.add(
                TerritoryAssignmentRow(
                    assignment_id=new_id("ter"),
                    tenant_id=str(tenant_id),
                    employee_id=str(employee_id),
                    priority=1,
                    effective_from=_SEED_TIME,
                    countries=[country],
                    product_categories=[],
                    need_categories=[category],
                    buyer_types=[],
                    languages=[],
                    manager_id=str(employees.manager),
                    backup_employee_id=None,
                    effective_until=None,
                )
            )
        await session.flush()
        await session.commit()
    finally:
        await session.close()


def _start_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout: BinaryIO,
    stderr: BinaryIO,
    pass_fds: tuple[int, ...] = (),
) -> ManagedProcess:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        pass_fds=pass_fds,
    )
    return ManagedProcess(
        process=process,
        stdout_path=Path(stdout.name),
        stderr_path=Path(stderr.name),
    )


@pytest_asyncio.fixture(scope="session")
async def e2e_stack() -> AsyncIterator[E2EStack]:
    if not await asyncio.to_thread(_docker_available):
        if os.environ.get("TRADEOS_REQUIRE_E2E") == "1":
            pytest.fail("Docker 不可用，必需的真实浏览器 E2E 无法启动")
        pytest.skip("Docker 不可用")

    temporary = TemporaryDirectory(prefix="tradeos-e2e-")
    temporary_path = Path(temporary.name)
    log_handles: list[BinaryIO] = []
    container = PostgresContainer(_CONTAINER_IMAGE)
    container_started = False
    engine: AsyncEngine | None = None
    api_process: ManagedProcess | None = None
    vite_process: ManagedProcess | None = None
    listener: socket.socket | None = None
    try:
        await asyncio.to_thread(container.start)
        container_started = True
        database_url = _to_asyncpg(container.get_connection_url())
        migration = await asyncio.to_thread(
            subprocess.run,
            ["alembic", "upgrade", "head"],
            cwd=_REPO_ROOT,
            env={**_minimal_process_env(), "DATABASE_URL": database_url},
            capture_output=True,
            check=False,
        )
        assert migration.returncode == 0, "alembic upgrade head 失败"

        engine = create_engine_from(database_url)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        tenant_id = TenantId(new_id("ten"))
        employees = E2EEmployees(
            boss=EmployeeId(new_id("emp")),
            manager=EmployeeId(new_id("emp")),
            sales_a=EmployeeId(new_id("emp")),
            sales_b=EmployeeId(new_id("emp")),
        )
        await _seed_employees_and_territories(factory, tenant_id, employees)

        vite_port = _free_port()
        web_origin = f"http://127.0.0.1:{vite_port}"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        api_port = int(listener.getsockname()[1])
        api_origin = f"http://127.0.0.1:{api_port}"
        runtime_env = _runtime_process_env(database_url, tenant_id, web_origin)
        runtime_settings = Phase1RuntimeSettings.from_environ(runtime_env)

        for name in ("api.stdout", "api.stderr", "vite.stdout", "vite.stderr"):
            log_handles.append((temporary_path / name).open("wb"))
        api_stdout, api_stderr, vite_stdout, vite_stderr = log_handles

        api_process = _start_process(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.api.runtime:create_runtime_app",
                "--factory",
                "--fd",
                str(listener.fileno()),
                "--no-access-log",
            ],
            cwd=_REPO_ROOT,
            env=runtime_env,
            stdout=api_stdout,
            stderr=api_stderr,
            pass_fds=(listener.fileno(),),
        )
        listener.close()
        listener = None
        vite_process = _start_process(
            [
                "npm",
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                str(vite_port),
                "--strictPort",
            ],
            cwd=_REPO_ROOT / "apps/web",
            env={
                **_minimal_process_env(),
                "VITE_API_BASE_URL": api_origin,
                "VITE_TENANT_ID": str(tenant_id),
                "VITE_EMPLOYEE_ID": str(employees.boss),
            },
            stdout=vite_stdout,
            stderr=vite_stderr,
        )
        await _wait_for_http(
            f"{api_origin}/health/ready",
            api_process,
            headers={"X-Tenant-Id": str(tenant_id)},
        )
        await _wait_for_http(
            f"{web_origin}/crm/opportunities",
            vite_process,
        )
        yield E2EStack(
            api_origin=api_origin,
            web_origin=web_origin,
            tenant_id=tenant_id,
            employees=employees,
            engine=engine,
            factory=factory,
            runtime_settings=runtime_settings,
            api_process=api_process,
            vite_process=vite_process,
        )
    finally:
        if listener is not None:
            listener.close()
        if vite_process is not None:
            await asyncio.to_thread(vite_process.stop)
        if api_process is not None:
            await asyncio.to_thread(api_process.stop)
        if engine is not None:
            await engine.dispose()
        for handle in log_handles:
            handle.close()
        temporary.cleanup()
        if container_started:
            await asyncio.to_thread(container.stop)
