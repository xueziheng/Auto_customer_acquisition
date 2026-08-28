"""本批专用Linux构建/容器编排；白名单输入、不读生产配置。"""

from __future__ import annotations

import io
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from uuid import uuid4

import docker
from testcontainers.core.config import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

PYTHON_IMAGE = "python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579"
TARGET_MANIFEST = (
    "sha256:457e0286fc132c4531ea071629ae6959095aa4074f172cd271aedb6950714ae6"
)


def build_parser_image(base_image: str | None = None, *, chain: bool = False) -> str:
    """显式构建入口，只在获准构建阶段联网获取已列依赖。"""
    root = Path(__file__).resolve().parents[2]
    paths = [
        root / "pyproject.toml",
        root / "connectors/__init__.py",
        root / "connectors/base.py",
    ]
    for package in ("shared", "connectors/evidence_text"):
        paths.extend(
            path
            for path in (root / package).rglob("*.py")
            if not path.name.startswith("._")
        )
    for name in (
        "tests/__init__.py",
        "tests/unit/__init__.py",
        "tests/integration/__init__.py",
        "tests/unit/test_evidence_text_profiles.py",
        "tests/unit/test_evidence_parser_client.py",
        "tests/integration/evidence_parser_linux_cases.py",
    ):
        path = root / name
        if path.exists():
            paths.append(path)
    if chain:
        if base_image is None:
            raise ValueError("全链构建必须显式复用已验证依赖产物")
        for package in (
            "connectors/contact_enrichment",
            "connectors/email_verification",
            "connectors/hunter",
            "connectors/gmail",
            "domains",
            "workflows",
            "tool_gateway",
            "artifact_store",
            "infra",
            "migrations",
        ):
            paths.extend(
                path
                for path in (root / package).rglob("*.py")
                if not path.name.startswith("._")
            )
        for name in (
            "alembic.ini",
            "scripts/run_alembic.py",
            "tests/integration/conftest.py",
            "tests/integration/quote_evidence_linux_support.py",
            "tests/integration/test_quote_evidence_gateway.py",
            "tests/integration/test_quote_source_readers.py",
            "tests/integration/test_need_units.py",
            "tests/unit/test_quote_evidence_contracts.py",
            "tests/unit/test_quote_evidence_access.py",
            "tests/unit/test_quote_evidence_gateway.py",
            "tests/unit/test_quote_source_readers.py",
            "tests/unit/test_need_units.py",
        ):
            paths.append(root / name)
    paths.append(root / "tests/fixtures/quote_evidence/linux/Dockerfile")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(set(paths)):
            entry = archive.gettarinfo(str(path), arcname=str(path.relative_to(root)))
            entry.mode = 0o644
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            with path.open("rb") as source:
                archive.addfile(entry, source)
    buffer.seek(0)
    client = docker.from_env()
    try:
        if base_image is not None:
            if not base_image.startswith("sha256:") or len(base_image) != 71:
                raise ValueError("测试基础产物必须为精确image ID")
            client.images.get(base_image)
        image, logs = client.images.build(
            fileobj=buffer,
            custom_context=True,
            dockerfile="tests/fixtures/quote_evidence/linux/Dockerfile",
            buildargs={
                "PYTHON_IMAGE": PYTHON_IMAGE,
                "TEST_BASE_IMAGE": base_image or PYTHON_IMAGE,
            },
            target="chain" if chain else "refreshed" if base_image else "parser",
            platform="linux/arm64",
            rm=True,
        )
        # 构建日志可能包含下载环境；只返回固定image ID，不打印任意底层日志。
        del logs
        return image.id
    except Exception:  # noqa: BLE001 - 构建环境错误仅固定消息
        raise RuntimeError("来源测试镜像构建失败（不输出构建环境）") from None
    finally:
        client.close()
        buffer.close()


def image_id() -> str:
    value = os.environ.get("QUOTE_EVIDENCE_IMAGE_ID", "")
    if not value.startswith("sha256:") or len(value) != 71:
        raise RuntimeError("not_run：缺少显式来源测试镜像ID")
    client = docker.from_env()
    try:
        image = client.images.get(value)
        if image.attrs["Architecture"] != "arm64" or image.attrs["Os"] != "linux":
            raise RuntimeError("not_run：测试镜像架构不匹配")
    finally:
        client.close()
    return value


def resource_container(value: str) -> DockerContainer:
    """不挂卷/端口/socket，network none且所有预算固定为测试专用值。"""
    return DockerContainer(
        value,
        network_mode="none",
        read_only=True,
        user="65534:65534",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        mem_limit=1073741824,
        memswap_limit=1073741824,
        pids_limit=128,
        nano_cpus=2000000000,
    ).with_tmpfs_mount("/tmp", "rw,size=67108864,noexec,nosuid")


def run_resource_cases() -> tuple[int, str]:
    previous = testcontainers_config.ryuk_disabled
    testcontainers_config.ryuk_disabled = True
    try:
        with resource_container(image_id()) as container:
            wrapped = container.get_wrapped_container()
            result = wrapped.wait(timeout=240)
            logs = wrapped.logs().decode("utf-8", errors="replace")
            return result["StatusCode"], logs
    finally:
        testcontainers_config.ryuk_disabled = previous


def run_chain_cases() -> tuple[int, str]:
    """仅自建internal网络两容器；临时凭证内部使用，finally精确回收。"""
    value = image_id()
    client = docker.from_env()
    network = None
    pg = runner = None
    previous = testcontainers_config.ryuk_disabled
    testcontainers_config.ryuk_disabled = True
    stage = "image"
    try:
        client.images.get("pgvector/pgvector:pg16")
        stage = "network"
        suffix = uuid4().hex
        network = Network(
            docker_network_kw={"driver": "bridge", "internal": True}
        ).create()
        username, database, password = "qe_test", "qe_test", secrets.token_hex(32)
        pg_name = "qe-pg-" + suffix
        stage = "pg_create"
        pg = (
            DockerContainer(
                "pgvector/pgvector:pg16",
                name=pg_name,
                mem_limit=536870912,
                memswap_limit=536870912,
            )
            .with_network(network)
            .with_network_aliases(pg_name)
        )
        pg.with_env("POSTGRES_USER", username).with_env(
            "POSTGRES_PASSWORD", password
        ).with_env("POSTGRES_DB", database)
        pg.start()
        stage = "pg_ready"
        deadline = time.monotonic() + 30
        while (
            pg.get_wrapped_container()
            .exec_run(["pg_isready", "-U", username, "-d", database])
            .exit_code
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError("not_run：临时数据库准备超时")
            time.sleep(0.2)
        connection = (
            f"postgresql+asyncpg://{username}:{password}@{pg_name}:5432/{database}"
        )
        stage = "runner_create"
        runner = (
            DockerContainer(
                value,
                read_only=True,
                user="65534:65534",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                mem_limit=1073741824,
                memswap_limit=1073741824,
                pids_limit=128,
                nano_cpus=2000000000,
            )
            .with_network(network)
            .with_tmpfs_mount("/tmp", "rw,size=67108864,noexec,nosuid")
        )
        runner.with_env("TEST_DATABASE_URL", connection).with_env(
            "PYTHON_DOTENV_DISABLED", "1"
        )
        runner.start()
        wrapped = runner.get_wrapped_container()
        wrapped.reload()
        host = wrapped.attrs["HostConfig"]
        assert (
            host["Memory"],
            host["MemorySwap"],
            host["PidsLimit"],
            host["NanoCpus"],
        ) == (1073741824, 1073741824, 128, 2000000000)
        assert (
            host["ReadonlyRootfs"] and wrapped.attrs["Config"]["User"] == "65534:65534"
        )
        assert host["CapDrop"] == ["ALL"] and "no-new-privileges" in host["SecurityOpt"]
        assert not host["Binds"] and not host["PortBindings"]
        assert host["Tmpfs"] == {"/tmp": "rw,size=67108864,noexec,nosuid"}
        assert client.networks.get(network.id).attrs["Internal"] is True
        assert set(wrapped.attrs["NetworkSettings"]["Networks"]) == {network.name}
        assert pg.get_wrapped_container().attrs["HostConfig"]["Memory"] == 536870912
        stage = "runner_wait"
        result = wrapped.wait(timeout=240)
        logs = wrapped.logs().decode("utf-8", errors="replace")
        return result["StatusCode"], logs.replace(
            connection, "<test database>"
        ).replace(password, "<test credential>")
    except Exception as error:  # noqa: BLE001 - fixture不传Docker/连接异常原文
        category = type(error).__name__
        if category not in {
            "ImageNotFound",
            "APIError",
            "TypeError",
            "AttributeError",
            "RuntimeError",
            "ReadTimeout",
        }:
            category = "environment_error"
        raise RuntimeError(
            f"not_run：来源Linux同链环境失败 {stage}/{category}"
        ) from None
    finally:
        try:
            if runner is not None:
                runner.stop()
        finally:
            try:
                if pg is not None:
                    pg.stop()
            finally:
                try:
                    if network is not None:
                        network.remove()
                finally:
                    client.close()
                    testcontainers_config.ryuk_disabled = previous


def _chain_main() -> int:
    """已安装runner入口：只迁移显式测试库，不继承其他凭证或运行安装。"""
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
        print("not_run：同链测试数据库迁移失败")
        return 2
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(root / "pyproject.toml"),
            "tests/unit/test_quote_source_readers.py",
            "tests/integration/test_quote_source_readers.py",
            "tests/unit/test_need_units.py",
            "tests/integration/test_need_units.py",
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
    # 不转发可能带原件/连接的任意traceback，只输出测试身份及计数。
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
            line
            and line[0].isdigit()
            and (
                " passed" in line
                or " failed" in line
                or " error" in line
                or " skipped" in line
            )
        ):
            print(line)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(_chain_main())
