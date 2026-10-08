"""在本地临时PostgreSQL中执行企业与平台真实隔离验收，不消费部署连接。"""

from __future__ import annotations

import io
import os
import re
import secrets
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from xml.etree import ElementTree

from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

_ROOT = Path(__file__).resolve().parents[1]
_FILES = (
    "tests/integration/test_tenant_row_security.py",
    "tests/integration/test_enterprise_api_isolation.py",
    "tests/integration/test_platform_access.py",
    "tests/integration/test_enterprise_knowledge_isolation.py",
)
_MODULES = frozenset(path.removesuffix(".py").replace("/", ".") for path in _FILES)
_TEST_NAME = re.compile(r"test_[A-Za-z0-9_]+")


def _safe_test_diagnostics(root: ElementTree.Element) -> tuple[str, ...]:
    """只返回固定模块、无参数的测试名与结果类别，不读取异常或任意属性。"""
    diagnostics: set[str] = set()
    for case in root.iter("testcase"):
        module = case.get("classname", "")
        name = case.get("name", "").partition("[")[0]
        if module not in _MODULES or _TEST_NAME.fullmatch(name) is None:
            continue
        for category in ("failure", "error", "skipped"):
            if case.find(category) is not None:
                diagnostics.add(f"{module}::{name} {category}")
    return tuple(sorted(diagnostics))


def main() -> int:
    """只注入本次容器URL；不输出子进程材料，失败与清理失败均不能伪报成功。"""
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host and not docker_host.startswith("unix://"):
        print("企业隔离验收失败：只允许本地 Docker。")
        return 2

    container = None
    code, passed = 2, 0
    diagnostics: tuple[str, ...] = ()
    stage = "Docker/PostgreSQL 启动"
    failure = None
    print("企业隔离验收：启动独立临时 PostgreSQL。", flush=True)
    # 容器启动异常、pytest输出及迁移材料不得把连接串带入CI日志。
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            database = "tradeos_isolation_test_" + uuid4().hex
            container = PostgresContainer(
                "pgvector/pgvector:pg16", dbname=database,
                username="isolation_admin", password=secrets.token_urlsafe(32),
            )
            container.start()
            url = container.get_connection_url(driver="asyncpg")
            parsed = make_url(url)
            if parsed.database != database or parsed.host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("隔离测试必须使用本次本地容器")
            env = dict(os.environ)
            for name in ("TEST_DATABASE_URL", "DATABASE_URL", "PYTEST_ADDOPTS"):
                env.pop(name, None)
            env["TRADEOS_ISOLATION_TEST_DATABASE_URL"] = url
            stage = "pytest 执行"
            with TemporaryDirectory(prefix="tradeos-isolation-results-") as directory:
                report = Path(directory) / "results.xml"
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", *_FILES, "-q", "--tb=no",
                     "-o", "addopts=", f"--junitxml={report}"],
                    cwd=_ROOT, env=env, capture_output=True, check=False, timeout=600,
                )
                code = result.returncode
                report_root = ElementTree.parse(report).getroot()
                diagnostics = _safe_test_diagnostics(report_root)
                if code == 0:
                    cases = list(report_root.iter("testcase"))
                    passed = len(cases)
                    if not cases or any(
                        case.find(tag) is not None
                        for case in cases for tag in ("skipped", "failure", "error")
                    ):
                        code, failure = 1, "测试未全部实际通过（存在跳过或空结果）"
        except KeyboardInterrupt:
            code, failure = 130, "验收被中断"
        except Exception:  # noqa: BLE001 -- 仅记录固定阶段，不输出连接或子进程异常材料
            code, failure = 2, stage + "失败"
        finally:
            if container is not None:
                try:
                    container.stop()
                except Exception:  # noqa: BLE001 -- 清理失败也必须失败且不回显容器材料
                    if code == 0:
                        code = 2
                    failure = "临时 PostgreSQL 清理失败"
    if code == 0:
        print(f"企业隔离验收通过：{passed} 项测试通过，临时 PostgreSQL 已回收。")
    elif failure is not None:
        print(f"企业隔离验收失败：{failure}，退出码 {code}。")
    else:
        print(f"企业隔离验收失败：pytest 退出码 {code}，临时 PostgreSQL 已回收。")
    for diagnostic in diagnostics:
        print(f"隔离测试结果：{diagnostic}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
