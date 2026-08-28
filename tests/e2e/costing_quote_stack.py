"""T10固定白名单构建和有限生命周期；仅本次internal网络/容器。"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
import time
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import docker

from tests.integration.quote_evidence_linux_support import build_parser_image

BASE_IMAGE = "sha256:2b0b00d817e767647f6d629902f5d391c13598cc481b75a5fabbaf7b2c229a33"


@lru_cache(maxsize=1)
def current_image():
    """当前白名单源码覆盖已验收依赖，不读取.env或联网装包。"""
    return build_parser_image(BASE_IMAGE, chain=True, quotation=True, costing_quote=True)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@dataclass
class LinuxStack:
    runner: object
    api_origin: str
    web_ports: tuple[int, ...]
    image: str

    def manifest(self):
        for line in self.runner.logs().decode("utf-8", errors="replace").splitlines():
            if line.startswith("t10_manifest="):
                value = json.loads(line.removeprefix("t10_manifest="))
                assert set(value) == {"tenant", "opportunity", "need", "source", "policy_source",
                                      "message", "actor", "boss", "decider", "statement", "now"}
                return value
        return None


def safe_output(raw):
    """只回传固定诊断/pytest摘要，不暴露Uvicorn和依赖任意日志。"""
    lines = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if (re.fullmatch(r"(?:runtime_http_error|fixed_exception_type|t10_fixture_error)=[A-Za-z0-9_.]+", line)
            or re.fullmatch(r"[a-zA-Z0-9_/]+\.py:[0-9]+: in [a-zA-Z0-9_]+", line)
            or re.fullmatch(r"t10_(?:worker_started_cycles|forbidden_gateway_calls)=[0-9]+", line)
            or re.fullmatch(r"(?:FAILED|ERROR) tests/[a-zA-Z0-9_/:.\[\]-]+", line)
            or re.fullmatch(r"[0-9]+ (?:passed|failed|error|errors)(?:, [0-9]+ (?:passed|failed|error|errors))* in [0-9.]+s", line)):
            lines.append(line)
    return "\n".join(lines)


@contextmanager
def linux_stack(*, mode="integration"):
    """固定mode，API仅loopback；所有ID来自本次创建并在finally收口。"""
    if mode not in {"integration", "browser", "visual"}:
        raise ValueError("固定测试模式无效")
    client = docker.from_env()
    network = pg = runner = None
    resource_ids = []
    port = 0  # Desktop internal网络不能实际publish；仅由固定host桥暴露loopback。
    web_ports = tuple(free_port() for _ in range(3)) if mode != "integration" else ()
    try:
        image = current_image()
        suffix = uuid4().hex
        network = client.networks.create("t10-" + suffix, internal=True, driver="bridge")
        password = secrets.token_hex(32)
        pg_name = "t10-pg-" + suffix
        pg = client.containers.run(
            "pgvector/pgvector:pg16", detach=True, network=network.name, name=pg_name,
            environment={"POSTGRES_USER": "t10", "POSTGRES_DB": "t10", "POSTGRES_PASSWORD": password},
            mem_limit=536870912, memswap_limit=536870912,
        )
        resource_ids.append(pg.id)
        deadline = time.monotonic() + 30
        while pg.exec_run(["pg_isready", "-U", "t10", "-d", "t10"]).exit_code:
            if time.monotonic() > deadline:
                raise AssertionError("T10隔离Postgres readiness超时")
            time.sleep(0.1)
        connection = f"postgresql+asyncpg://t10:{password}@{pg_name}:5432/t10"
        runner = client.containers.run(
            image, ["python", "-m", "tests.e2e.costing_quote_server", "--mode", mode],
            detach=True, network=network.name, read_only=True, user="65534:65534",
            cap_drop=["ALL"], security_opt=["no-new-privileges"],
            mem_limit=1073741824, memswap_limit=1073741824, pids_limit=128,
            nano_cpus=2000000000, tmpfs={"/tmp": "rw,size=67108864,noexec,nosuid"},
            ports={"8000/tcp": ("127.0.0.1", port)} if port else {},
            environment={"TEST_DATABASE_URL": connection, "PYTHON_DOTENV_DISABLED": "1",
                         "T10_CORS_ORIGINS": json.dumps([f"http://127.0.0.1:{p}" for p in web_ports])},
        )
        resource_ids.append(runner.id)
        runner.reload()
        host = runner.attrs["HostConfig"]
        assert host["ReadonlyRootfs"] and not host["Binds"]
        assert host["Memory"] == 1073741824 and host["PidsLimit"] == 128
        assert client.networks.get(network.id).attrs["Internal"]
        assert set(runner.attrs["NetworkSettings"]["Networks"]) == {network.name}
        if port:
            assert host["PortBindings"]["8000/tcp"] == [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
        else:
            assert not host["PortBindings"]
        yield LinuxStack(runner, f"http://127.0.0.1:{port}" if port else "", web_ports, image)
    finally:
        try:
            if runner is not None:
                runner.stop(timeout=12)
                for line in safe_output(runner.logs()).splitlines():
                    if line.startswith(("t10_worker_started_cycles=", "t10_forbidden_gateway_calls=")):
                        print(line)
                runner.remove()
        finally:
            try:
                if pg is not None:
                    pg.stop(timeout=5)
                    pg.remove()
            finally:
                if network is not None:
                    network.remove()
                try:
                    existing = {item.id for item in client.containers.list(all=True)}
                    assert not existing.intersection(resource_ids)
                    if port:
                        with socket.socket() as probe:
                            assert probe.connect_ex(("127.0.0.1", port)) != 0
                finally:
                    client.close()


def run_closed_loop():
    """固定Linux测试入口；不把容器环境/连接/任意日志回传模型。"""
    with linux_stack() as stack:
        result = stack.runner.wait(timeout=300)
        output = safe_output(stack.runner.logs())
        return result["StatusCode"], output


async def visual_handoff():
    """仅经controller协调启动：自动跑同DOM后保留本次栈，总计不超过900秒。"""
    import os
    import signal
    from datetime import UTC, datetime, timedelta

    from tests.e2e.test_costing_quote_browser import exercise_browser

    artifacts = Path(__file__).resolve().parents[2] / "output/playwright" / ("t10-visual-" + uuid4().hex)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for kind in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(kind, stop.set)
    try:
        try:
            async with browser_stack(artifacts, mode="visual") as stack:
                result = await exercise_browser(stack, artifacts)
                handoff = {"pid": os.getpid(), "artifacts": str(artifacts),
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=max(0, stack.deadline - loop.time()))).isoformat(),
                    "origins": result["origins"], "api_origin": stack.api_origin,
                    "quote_url": result["quote_url"], "quote_id": result["quote_id"],
                    "older_quote_id": result["older_quote_id"], "message_id": result["message"],
                    "source": result["source"], "policy_source": result["policy_source"],
                    "pdf_sha256": result["pdf_sha256"]}
                (artifacts / "handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2))
                print("t10_visual_handoff=" + json.dumps(handoff, ensure_ascii=False), flush=True)
                await stop.wait()
        except TimeoutError:
            print("t10_visual_deadline=expired", flush=True)
        print("t10_visual_cleanup=verified", flush=True)
    finally:
        for kind in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(kind)


@asynccontextmanager
async def browser_stack(artifacts: Path, *, mode="browser"):
    """三个固定开发身份只在Vite环境；不修改Browser身份或生产认证。"""
    from tests.e2e.conftest import _minimal_process_env, _start_process, _wait_for_http
    from tests.e2e.costing_quote_bridge import http_bridge

    deadline_total = asyncio.get_running_loop().time() + (900 if mode == "visual" else 300)
    artifacts.mkdir(parents=True, exist_ok=False)
    processes = []
    with linux_stack(mode=mode) as stack, ExitStack() as files:
        deadline = time.monotonic() + 45
        while True:
            manifest = stack.manifest()
            if manifest is not None:
                break
            stack.runner.reload()
            if stack.runner.status == "exited" or time.monotonic() > deadline:
                raise AssertionError("T10公开前置未就绪：" + safe_output(stack.runner.logs()))
            await asyncio.sleep(0.1)
        try:
            stack.api_origin = files.enter_context(http_bridge(stack.runner))
            origins = {}
            for role, port in zip(("boss", "actor", "decider"), stack.web_ports, strict=True):
                origin = f"http://127.0.0.1:{port}"
                process = _start_process(
                    ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1",
                     "--port", str(port), "--strictPort"],
                    cwd=Path(__file__).resolve().parents[2] / "apps/web",
                    env={**_minimal_process_env(), "PYTHON_DOTENV_DISABLED": "1",
                         "VITE_API_BASE_URL": stack.api_origin, "VITE_TENANT_ID": manifest["tenant"],
                         "VITE_EMPLOYEE_ID": manifest[role]},
                    stdout=files.enter_context((artifacts / f"{role}.stdout").open("wb")),
                    stderr=files.enter_context((artifacts / f"{role}.stderr").open("wb")),
                )
                processes.append(process)
                await _wait_for_http(origin + "/costing-quotes", process)
                origins[role] = origin
            stack.manifest_data = manifest
            stack.web_origins = origins
            stack.deadline = deadline_total
            # 真实API已启动，明确tenant与身份检查，不能用Vite HTML代替后端ready。
            import httpx
            async with httpx.AsyncClient() as client:
                async with asyncio.timeout(15):
                    while True:
                        try:
                            response = await client.get(stack.api_origin + "/costing-quotes/opportunities/"
                                + manifest["opportunity"] + "/quote-context", headers={
                                    "X-Tenant-Id": manifest["tenant"], "X-Employee-Id": manifest["boss"],
                                })
                            break
                        except httpx.ConnectError:
                            stack.runner.reload()
                            assert stack.runner.status == "running", safe_output(stack.runner.logs())
                            await asyncio.sleep(0.1)
                assert response.status_code == 200 and response.json()["blockers"] == []
            async with asyncio.timeout_at(deadline_total):
                yield stack
        finally:
            for process in reversed(processes):
                process.stop()
            for port in stack.web_ports:
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("visual",), required=True)
    parser.parse_args()
    asyncio.run(visual_handoff())
