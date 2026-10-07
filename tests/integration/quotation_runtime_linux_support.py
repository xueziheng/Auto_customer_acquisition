"""固定B2容器入口，复用A的隔离网络/PG/资源边界，不接受任意命令或路径。"""

import os
import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    connection = os.environ.get("TEST_DATABASE_URL")
    if not connection:
        print("not_run：缺少显式测试连接")
        return 2
    root = Path(__file__).resolve().parents[2]
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHON_DOTENV_DISABLED": "1",
        "TEST_DATABASE_URL": connection,
    }
    migrated = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", "upgrade", "head"],
        cwd=root,
        env={**env, "DATABASE_URL": connection},
        capture_output=True,
        timeout=60,
        check=False,
    )
    if migrated.returncode:
        print("not_run：报价同链测试数据库迁移失败")
        return 2
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(root / "pyproject.toml"),
            "tests/integration/quotation_runtime_linux_cases.py",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=180,
        check=False,
    )
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith(("FAILED tests/", "ERROR tests/")):
            print(line.split(" - ", 1)[0])
        elif re.fullmatch(r"[a-zA-Z0-9_/]+\.py:[0-9]+: in [a-zA-Z0-9_]+", line):
            print(line)
        elif match := re.match(
            r"E\s+([a-zA-Z][a-zA-Z0-9_.]*(?:Error|Exception)):", line
        ):
            print("fixed_exception_type=" + match[1])
        elif (
            re.fullmatch(
                r"runtime_(?:http_error|state_(?:step|status|outcome|error|exception)|package_count)=[a-zA-Z0-9_]+",
                line,
            )
            or line
            and line[0].isdigit()
            and any(
                word in line for word in (" passed", " failed", " error", " skipped")
            )
        ):
            print(line)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
