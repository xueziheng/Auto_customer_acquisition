"""本批专用Linux构建/容器编排；白名单输入、不读生产配置。"""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import docker
from testcontainers.core.config import testcontainers_config
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network

PYTHON_IMAGE = "python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579"
_SOURCE_MANIFEST_LABEL = "org.tradeos.source-manifest-sha256"
_SOURCE_PLATFORM_LABEL = "org.tradeos.source-platform"
_SOURCE_PLATFORM = "linux/arm64"
_DEPENDENCY_ONLY_LABEL = "org.tradeos.dependency-only"
_DEPENDENCY_BASE_LABEL = "org.tradeos.dependency-base"
_DEPENDENCY_LOCK_LABEL = "org.tradeos.dependency-lock-sha256"
_DEPENDENCY_MANIFEST_LABEL = "org.tradeos.dependency-manifest-sha256"
_DEPENDENCY_PLATFORM_LABEL = "org.tradeos.dependency-platform"
_DEPENDENCY_LOCK_PATH = Path("tests/fixtures/quote_evidence/linux/requirements.lock")
_AUDITED_DEPENDENCY_MANIFEST_SHA256 = (
    "5f1de242e69d0476eee532fa42fd6b5ea396997a065525162cd242b70091029b"
)
_REQUIRED_DEPENDENCY_MODULES = (
    "alembic",
    "docker",
    "pydantic",
    "pytest",
    "sqlalchemy",
    "testcontainers",
)
_TRADEOS_SOURCE_ROOTS = (
    "apps",
    "domains",
    "shared",
    "connectors",
    "workflows",
    "tool_gateway",
    "artifact_store",
    "infra",
    "migrations",
    "agent_runtime",
    "notification_gateway",
    "tests",
)


def _repository_root() -> Path:
    """返回本受限构建唯一允许读取的仓库根目录。"""

    return Path(__file__).resolve().parents[2]


def dependency_lock_sha256() -> str:
    """锁文件的内容地址；不从环境、镜像层或本机包缓存推导。"""

    lock_path = _repository_root() / _DEPENDENCY_LOCK_PATH
    with lock_path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _dependency_labels() -> dict[str, str]:
    """依赖产物的可审计身份；只允许固定 Python 根层和锁文件。"""

    return {
        _DEPENDENCY_ONLY_LABEL: "true",
        _DEPENDENCY_BASE_LABEL: PYTHON_IMAGE,
        _DEPENDENCY_LOCK_LABEL: dependency_lock_sha256(),
        _DEPENDENCY_MANIFEST_LABEL: _AUDITED_DEPENDENCY_MANIFEST_SHA256,
        _DEPENDENCY_PLATFORM_LABEL: _SOURCE_PLATFORM,
    }


def dependency_bootstrap_image_tag() -> str:
    """显式 bootstrap 写入的唯一 artifact 标签；不枚举 Docker 本机镜像。"""

    return "tradeos-test-dependency:quote-evidence-linux-" + dependency_lock_sha256()[:16]


def _tar_context(root: Path, paths: tuple[Path, ...]) -> io.BytesIO:
    """以稳定元数据打包明确白名单，拒绝把工作树其余内容交给 Docker。"""

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in paths:
            entry = archive.gettarinfo(str(path), arcname=str(path.relative_to(root)))
            entry.mode = 0o644
            entry.mtime = 0
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            with path.open("rb") as source:
                archive.addfile(entry, source)
    buffer.seek(0)
    return buffer


def _source_paths(
    root: Path, *, chain: bool, quotation: bool, costing_quote: bool
) -> tuple[Path, ...]:
    """只列出 runner 真实读取的源码；不把工作树状态或凭证带进构建上下文。"""

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
            "tests/integration/quote_source_readers_linux_cases.py",
            "tests/integration/test_need_units.py",
            "tests/unit/test_quote_evidence_contracts.py",
            "tests/unit/test_quote_evidence_access.py",
            "tests/unit/test_quote_evidence_gateway.py",
            "tests/unit/test_quote_source_readers.py",
            "tests/unit/test_need_units.py",
        ):
            paths.append(root / name)
    if quotation:
        for package in (
            "apps/api",
            "apps/scheduler_worker",
            "apps/notification_worker",
            "agent_runtime",
            "notification_gateway",
            "connectors/dns_auth",
            "connectors/object_store",
            "connectors/openai",
            "connectors/quote_pdf",
            "connectors/tavily",
            "connectors/web_search",
        ):
            paths.extend(
                path
                for path in (root / package).rglob("*.py")
                if not path.name.startswith("._")
            )
        if (root / "apps/__init__.py").is_file():
            paths.append(root / "apps/__init__.py")
        for name in (
            "apps/composition_support/__init__.py",
            "apps/composition_support/quotations.py",
            "connectors/search_contracts.py",
            "tests/integration/test_quote_runtime.py",
            "tests/integration/test_api_runtime.py",
            "tests/integration/test_scheduler_worker.py",
            "tests/unit/test_api_runtime_config.py",
            "tests/quotation_runtime_fixtures.py",
            "tests/integration/quotation_runtime_linux_cases.py",
            "tests/integration/quotation_runtime_linux_support.py",
        ):
            paths.append(root / name)
    if costing_quote:
        for name in (
            "tests/integration/costing_quote_case.py",
            "tests/integration/test_costing_quote_closed_loop.py",
            "tests/e2e/costing_quote_server.py",
            "tests/e2e/costing_quote_relay.py",
        ):
            paths.append(root / name)
    paths.extend(
        (
            root / "tests/fixtures/quote_evidence/linux/Dockerfile",
            root / _DEPENDENCY_LOCK_PATH,
        )
    )
    return tuple(sorted(set(paths)))


def _manifest_sha256(root: Path, paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as source:
            digest.update(hashlib.file_digest(source, "sha256").digest())
    return digest.hexdigest()


def source_manifest_sha256(
    *, chain: bool = True, quotation: bool = False, costing_quote: bool = False
) -> str:
    """当前白名单源码的稳定内容 manifest；不包含 mtime、镜像 ID 或环境变量。"""

    root = Path(__file__).resolve().parents[2]
    return _manifest_sha256(
        root,
        _source_paths(
            root, chain=chain, quotation=quotation, costing_quote=costing_quote
        ),
    )


def _dependency_manifest_sha256(client: Any, image_id: str) -> str:
    """在 network none 的一次性容器中读取依赖清单 hash，不暴露清单文本。"""

    containers = client.containers
    output = containers.run(
        image_id,
        ["-m", "pip", "freeze", "--all"],
        entrypoint=["python"],
        network_mode="none",
        remove=True,
        user="65534:65534",
        read_only=True,
    )
    if not isinstance(output, bytes):
        raise TypeError("依赖镜像清单输出无效")
    lines = sorted(
        line.strip() for line in output.decode("utf-8", errors="strict").splitlines()
    )
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _dependency_runtime_is_complete(client: Any, image_id: str) -> bool:
    """验证候选可运行本 Linux 门禁所需模块；失败候选不参与来源层重建。"""

    try:
        client.containers.run(
            image_id,
            ["-c", "import " + ", ".join(_REQUIRED_DEPENDENCY_MODULES)],
            entrypoint=["python"],
            network_mode="none",
            remove=True,
            user="65534:65534",
            read_only=True,
        )
    except docker.errors.ContainerError:
        return False
    return True


def _dependency_image_is_source_free(client: Any, image_id: str) -> bool:
    """确认 bootstrap 不含任一 TradeOS 源码根或已安装项目 distribution。"""

    program = (
        "from importlib.metadata import distributions; "
        "from importlib.util import find_spec; from pathlib import Path; "
        "from sys import path as sys_path; "
        f"roots={_TRADEOS_SOURCE_ROOTS!r}; "
        "assert not Path('/opt/tradeos').exists(); "
        "assert not any((Path('/opt/tradeos') / root).exists() for root in roots); "
        "assert not any(Path('/').joinpath(root).exists() for root in roots); "
        "assert all(find_spec(root) is None for root in roots); "
        "assert not any(Path(entry, root).exists() for entry in sys_path if entry "
        "for root in roots); "
        "assert not any((item.metadata['Name'] or '').lower() == 'tradeos-agent' "
        "for item in distributions()); print('dependency-only')"
    )
    try:
        output = client.containers.run(
            image_id,
            ["-c", program],
            entrypoint=["python"],
            network_mode="none",
            remove=True,
            user="65534:65534",
            read_only=True,
        )
    except docker.errors.ContainerError:
        return False
    return output == b"dependency-only\n"


def _has_pinned_python_rootfs_prefix(client: Any, image: Any) -> bool:
    """候选 RootFS 必须以本地 pinned Python 基础镜像的真实层序列开头。"""

    try:
        pinned_python = client.images.get(PYTHON_IMAGE)
    except docker.errors.ImageNotFound:
        return False
    candidate_layers = image.attrs.get("RootFS", {}).get("Layers")
    pinned_layers = pinned_python.attrs.get("RootFS", {}).get("Layers")
    if not isinstance(candidate_layers, list) or not isinstance(pinned_layers, list):
        return False
    if not pinned_layers or len(candidate_layers) < len(pinned_layers):
        return False
    return candidate_layers[:len(pinned_layers)] == pinned_layers


def bootstrap_dependency_image() -> str:
    """仅按仓库内带 hash 的公开依赖锁构建 dependency-only Linux/arm64 产物。

    这是唯一允许联网的开发 bootstrap；所有源码层和正式 Linux 用例均继续
    ``network_mode=none``。上下文不包含 TradeOS 源码，镜像也不会安装本项目。
    """

    root = _repository_root()
    paths = (
        root / "tests/fixtures/quote_evidence/linux/Dockerfile",
        root / _DEPENDENCY_LOCK_PATH,
    )
    buffer = _tar_context(root, paths)
    client = docker.from_env()
    try:
        image, logs = client.images.build(
            fileobj=buffer,
            custom_context=True,
            dockerfile="tests/fixtures/quote_evidence/linux/Dockerfile",
            buildargs={"PYTHON_IMAGE": PYTHON_IMAGE},
            target="dependency",
            platform=_SOURCE_PLATFORM,
            network_mode="default",
            labels=_dependency_labels(),
            tag=dependency_bootstrap_image_tag(),
            rm=True,
        )
        del logs
        image_id = image.id
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise RuntimeError("dependency-only bootstrap 未返回精确 image ID")
        return image_id
    except Exception as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError("dependency-only bootstrap 构建失败") from None
    finally:
        client.close()
        buffer.close()


def audited_dependency_image_id() -> str:
    """验证显式 bootstrap 产物；绝不联网、枚举或猜测本机其他镜像。"""

    client = docker.from_env()
    try:
        try:
            image = client.images.get(dependency_bootstrap_image_tag())
        except docker.errors.ImageNotFound:
            raise RuntimeError(
                "缺少 dependency-only bootstrap artifact；先显式运行 bootstrap"
            ) from None
        image_id = image.id
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise RuntimeError("dependency-only bootstrap artifact 没有精确 image ID")
        attrs = image.attrs
        labels = attrs.get("Config", {}).get("Labels") or {}
        if (
            attrs.get("Architecture") != "arm64"
            or attrs.get("Os") != "linux"
            or any(labels.get(name) != value for name, value in _dependency_labels().items())
        ):
            raise RuntimeError("dependency-only bootstrap 标签或平台不匹配")
        if not _has_pinned_python_rootfs_prefix(client, image):
            raise RuntimeError("dependency-only bootstrap pinned Python RootFS 前缀不匹配")
        if not _dependency_image_is_source_free(client, image_id):
            raise RuntimeError("dependency-only bootstrap 含项目源码或 distribution")
        if not _dependency_runtime_is_complete(client, image_id):
            raise RuntimeError("dependency-only bootstrap 缺少 Linux runner 依赖")
        if _dependency_manifest_sha256(client, image_id) != _AUDITED_DEPENDENCY_MANIFEST_SHA256:
            raise RuntimeError("dependency-only bootstrap 依赖 manifest 不匹配")
        return image_id
    finally:
        client.close()


def build_parser_image(
    base_image: str | None = None, *, chain: bool = False, quotation: bool = False,
    costing_quote: bool = False,
) -> str:
    """显式构建入口，只在获准构建阶段联网获取已列依赖。"""
    if costing_quote and (not quotation or not chain or base_image is None):
        raise ValueError("T10必须显式复用quotation/chain和已验收image")
    if quotation and (not chain or base_image is None):
        raise ValueError("报价同链必须显式复用chain和已验收image")
    root = _repository_root()
    paths = _source_paths(
        root, chain=chain, quotation=quotation, costing_quote=costing_quote
    )
    source_manifest = _manifest_sha256(root, paths)
    buffer = _tar_context(root, paths)
    client = docker.from_env()
    phase = "base_lookup"
    try:
        if base_image is not None:
            if not base_image.startswith("sha256:") or len(base_image) != 71:
                raise ValueError("测试基础产物必须为精确image ID")
            client.images.get(base_image)
        else:
            client.images.get(PYTHON_IMAGE)
        phase = "docker_build"
        image, logs = client.images.build(
            fileobj=buffer,
            custom_context=True,
            dockerfile="tests/fixtures/quote_evidence/linux/Dockerfile",
            buildargs={
                "PYTHON_IMAGE": PYTHON_IMAGE,
                "TEST_BASE_IMAGE": base_image or PYTHON_IMAGE,
            },
            target="costing_quote"
            if costing_quote
            else "quotation"
            if quotation
            else "chain"
            if chain
            else "refreshed"
            if base_image
            else "parser",
            platform=_SOURCE_PLATFORM,
            network_mode="none",
            labels={
                _SOURCE_MANIFEST_LABEL: source_manifest,
                _SOURCE_PLATFORM_LABEL: _SOURCE_PLATFORM,
            },
            rm=True,
        )
        # 构建日志可能包含下载环境；只返回固定image ID，不打印任意底层日志。
        del logs
        return image.id
    except Exception as error:  # noqa: BLE001 - 构建环境错误仅固定消息
        if costing_quote:
            print("t10_build_failure_phase=" + phase)
            category = type(error).__name__
            if category not in {"BuildError", "APIError", "DockerException", "ImageNotFound",
                                "NotFound", "ConnectionError", "ReadTimeout", "Timeout"}:
                category = "unknown"
            print("t10_build_failure_type=" + category)
        raise RuntimeError("来源测试镜像构建失败（不输出构建环境）") from None
    finally:
        client.close()
        buffer.close()


def image_id() -> str:
    """离线重建当前源码层，并验证其源码 manifest 与 Linux 平台。"""

    dependency_image = audited_dependency_image_id()
    value = build_parser_image(dependency_image, chain=True)
    manifest = source_manifest_sha256(chain=True)
    client = docker.from_env()
    try:
        image = client.images.get(value)
        if image.attrs["Architecture"] != "arm64" or image.attrs["Os"] != "linux":
            raise RuntimeError("not_run：测试镜像架构不匹配")
        labels = image.attrs.get("Config", {}).get("Labels") or {}
        if (
            labels.get(_SOURCE_MANIFEST_LABEL) != manifest
            or labels.get(_SOURCE_PLATFORM_LABEL) != _SOURCE_PLATFORM
        ):
            raise RuntimeError("not_run：来源测试镜像 manifest 不匹配")
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
        with resource_container(image_id()).with_command(
            [
                "python",
                "-m",
                "pytest",
                "-c",
                "/opt/tradeos/pyproject.toml",
                "/usr/local/lib/python3.12/site-packages/tests/integration/evidence_parser_linux_cases.py",
                "/usr/local/lib/python3.12/site-packages/tests/unit/test_evidence_text_profiles.py",
                "/usr/local/lib/python3.12/site-packages/tests/unit/test_evidence_parser_client.py",
                "-q",
                "--tb=short",
                "-p",
                "no:cacheprovider",
            ]
        ) as container:
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
        username, database, pg_credential = "qe_test", "qe_test", secrets.token_hex(32)
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
            "POSTGRES_PASSWORD", pg_credential
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
        connection = f"postgresql+asyncpg://{username}" + f":{pg_credential}@{pg_name}:5432/{database}"
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
        runner.with_command(["python", "-m", "tests.integration.quote_evidence_linux_support"])
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
        ).replace(pg_credential, "<test credential>")
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
            "tests/integration/quote_source_readers_linux_cases.py",
            "tests/unit/test_need_units.py",
            "tests/integration/test_need_units.py",
            "-vv",
            "-rA",
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
        if match := re.fullmatch(
            r"(?:PASSED tests/integration/quote_source_readers_linux_cases\.py::"
            r"(test_actual_parser_prices_and_unit_receipt_history|"
            r"test_clipped_source_is_rejected_by_real_linux_need_confirmation)"
            r"(?:\[.*\])?|tests/integration/quote_source_readers_linux_cases\.py::"
            r"(test_actual_parser_prices_and_unit_receipt_history|"
            r"test_clipped_source_is_rejected_by_real_linux_need_confirmation)"
            r"(?:\[.*\])? PASSED)", line,
        ):
            case_name = match[1] or match[2]
            print("source_linux_case_passed=" + {
                "test_actual_parser_prices_and_unit_receipt_history": "prices_and_unit_history",
                "test_clipped_source_is_rejected_by_real_linux_need_confirmation": "clipped_source_rejected",
            }[case_name])
        elif line.startswith(("FAILED tests/", "ERROR tests/")):
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
