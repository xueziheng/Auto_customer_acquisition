"""本批专用Linux构建/容器编排；白名单输入、不读生产配置。"""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import docker
from testcontainers.core.config import testcontainers_config
from testcontainers.core.container import DockerContainer

PYTHON_IMAGE = "python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579"
TARGET_MANIFEST = (
    "sha256:457e0286fc132c4531ea071629ae6959095aa4074f172cd271aedb6950714ae6"
)


def build_parser_image(base_image: str | None = None) -> str:
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
            target="refreshed" if base_image else "parser",
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
