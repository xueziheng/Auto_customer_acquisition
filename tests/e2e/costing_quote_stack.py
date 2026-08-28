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
            or re.fullmatch(r"t10_unit_diag_(?:(?:requests|failed|responses|selection_start|selection_end|text_length)=[0-9]+|disabled=[01])", line)
            or re.fullmatch(r"t10_unit_probe_(?:initial_response|failed|response|summary_visible|disabled)=[01]", line)
            or re.fullmatch(r"(?:FAILED|ERROR) tests/[a-zA-Z0-9_/:.\[\]-]+", line)
            or re.fullmatch(r"[0-9]+ (?:passed|failed|error|errors)(?:, [0-9]+ (?:passed|failed|error|errors))* in [0-9.]+s", line)):
            lines.append(line)
    return "\n".join(lines)


@contextmanager
def linux_stack(*, mode="integration"):
    """固定mode，API仅loopback；所有ID来自本次创建并在finally收口。"""
    if mode not in {"integration", "browser", "visual"}:
        raise ValueError("固定测试模式无效")
    from tests.e2e.costing_quote_lifecycle import (
        OWNER_LABEL,
        active_run,
        resource_names,
    )

    ownership = active_run()
    suffix = ownership.owner if ownership else uuid4().hex
    names, labels = resource_names(suffix), {OWNER_LABEL: suffix}
    client = docker.from_env()
    network = pg = runner = None
    resource_ids = []
    primary = None
    port = 0  # Desktop internal网络不能实际publish；仅由固定host桥暴露loopback。
    web_ports = tuple(free_port() for _ in range(3)) if mode != "integration" else ()
    if ownership:
        (ownership.artifacts / "ports.json").write_text(json.dumps(web_ports))
    try:
        image = current_image()
        network = client.networks.create(names["network"], internal=True, driver="bridge", labels=labels)
        password = secrets.token_hex(32)
        pg_name = names["pg"]
        pg = client.containers.run(
            "pgvector/pgvector:pg16", detach=True, network=network.name, name=pg_name, labels=labels,
            environment={"POSTGRES_USER": "t10", "POSTGRES_DB": "t10", "POSTGRES_PASSWORD": password},
            mem_limit=536870912, memswap_limit=536870912,
        )
        resource_ids.append(pg.id)
        deadline = time.monotonic() + 30
        if ownership:
            deadline = min(deadline, ownership.work_deadline)
        while pg.exec_run(["pg_isready", "-U", "t10", "-d", "t10"]).exit_code:
            if time.monotonic() > deadline:
                raise AssertionError("T10隔离Postgres readiness超时")
            time.sleep(0.1)
        connection = f"postgresql+asyncpg://t10:{password}@{pg_name}:5432/t10"
        runner = client.containers.run(
            image, ["python", "-m", "tests.e2e.costing_quote_server", "--mode", mode],
            detach=True, network=network.name, name=names["api"], labels=labels,
            read_only=True, user="65534:65534",
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
    except BaseException as error:
        primary = error
        raise
    finally:
        failures = []

        def cleanup(action):
            try:
                action()
            except BaseException as error:  # noqa: BLE001 - 每项清理都尝试，最终保留主异常
                failures.append(error)

        if runner is not None:
            def finish_runner():
                runner.stop(timeout=12)
                runner.reload()
                state = runner.attrs["State"]
                assert state.get("Status") == "exited" and state.get("ExitCode") == 0
                assert not state.get("OOMKilled", False)
                lines = runner.logs().decode("utf-8", errors="replace").splitlines()
                if mode in {"browser", "visual"}:
                    assert lines.count("t10_runtime_exit=verified") == 1
                    assert lines.count("t10_forbidden_gateway_calls=0") == 1
                    assert sum(bool(re.fullmatch(r"t10_worker_started_cycles=[1-9][0-9]*", line))
                               for line in lines) == 1
                    assert not any(line.startswith("t10_fixture_error=") for line in lines)
                for line in lines:
                    if re.fullmatch(r"t10_(?:worker_started_cycles|forbidden_gateway_calls)=[0-9]+", line):
                        print(line)

            cleanup(finish_runner)
            cleanup(lambda: runner.remove(force=True))
        if pg is not None:
            cleanup(lambda: pg.stop(timeout=5))
            cleanup(lambda: pg.remove(force=True))
        if network is not None:
            cleanup(network.remove)

        def verify_removed():
            existing = {item.id for item in client.containers.list(all=True)}
            assert not existing.intersection(resource_ids)
            if port:
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0

        cleanup(verify_removed)
        cleanup(client.close)
        if failures:
            if primary is not None:
                primary.add_note("T10退出验收或清理失败；原主异常保留")
            else:
                raise AssertionError("T10退出验收或清理失败") from None


def run_closed_loop():
    """兼容同步调用方，但实际全部准备/执行在可终止子进程中。"""
    from tests.e2e.costing_quote_lifecycle import run_supervised

    result = asyncio.run(run_supervised("integration"))
    return result.code, result.output


def _run_closed_loop():
    """固定Linux测试入口；不把容器环境/连接/任意日志回传模型。"""
    from tests.e2e.costing_quote_lifecycle import active_run

    ownership = active_run()
    assert ownership is not None
    with linux_stack() as stack:
        result = stack.runner.wait(timeout=max(.1, ownership.work_deadline - time.monotonic()))
        output = safe_output(stack.runner.logs())
        return result["StatusCode"], output


async def visual_handoff():
    """仅经controller协调启动：自动跑同DOM后保留本次栈，总计不超过900秒。"""
    import signal
    from datetime import UTC, datetime, timedelta

    from tests.e2e.costing_quote_lifecycle import active_run
    from tests.e2e.test_costing_quote_browser import exercise_browser

    ownership = active_run()
    assert ownership is not None
    artifacts = ownership.artifacts
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for kind in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(kind, stop.set)
    try:
        try:
            async with browser_stack(artifacts, mode="visual") as stack:
                result = await exercise_browser(stack, artifacts)
                handoff = {"pid": ownership.parent_pid, "artifacts": str(artifacts),
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=max(0, stack.deadline - loop.time()))).isoformat(),
                    "origins": result["origins"], "api_origin": stack.api_origin,
                    "quote_url": result["quote_url"], "quote_id": result["quote_id"],
                    "older_quote_id": result["older_quote_id"], "message_id": result["message"],
                    "source": result["source"], "policy_source": result["policy_source"],
                    "pdf_sha256": result["pdf_sha256"]}
                (artifacts / "handoff.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2))
                print("t10_visual_handoff=ready", flush=True)
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
    from tests.e2e.conftest import _minimal_process_env, _start_process
    from tests.e2e.costing_quote_bridge import http_bridge
    from tests.e2e.costing_quote_lifecycle import active_run

    ownership = active_run()
    assert ownership is not None and artifacts == ownership.artifacts
    deadline_total = ownership.work_deadline
    processes = []
    with linux_stack(mode=mode) as stack, ExitStack() as files:
        deadline = min(time.monotonic() + 45, deadline_total)
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
            (artifacts / "ports.json").write_text(json.dumps([
                *stack.web_ports, int(stack.api_origin.rsplit(":", 1)[1]),
            ]))
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
                await _wait_for_ready(origin + "/costing-quotes", process, deadline_total)
                origins[role] = origin
            stack.manifest_data = manifest
            stack.web_origins = origins
            stack.deadline = deadline_total
            # 真实API已启动，明确tenant与身份检查，不能用Vite HTML代替后端ready。
            import httpx
            async with httpx.AsyncClient() as client:
                async with asyncio.timeout_at(min(deadline_total, time.monotonic() + 15)):
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
            failures = []
            for process in reversed(processes):
                try:
                    process.stop()
                except BaseException as error:  # noqa: BLE001 - 清理其他Vite后保留主异常
                    failures.append(error)
            for port in stack.web_ports:
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        failures.append(AssertionError("T10 Vite端口未回收"))
            if failures:
                import sys
                primary = sys.exc_info()[1]
                if primary is not None:
                    primary.add_note("T10 Vite清理失败；原主异常保留")
                else:
                    raise AssertionError("T10 Vite清理失败") from None


async def _wait_for_ready(url, process, deadline):
    """T10专用readiness使用剩余工作预算，不改变其他E2E的共享helper。"""
    import httpx

    deadline = min(deadline, time.monotonic() + 20)
    async with asyncio.timeout_at(deadline), httpx.AsyncClient() as client:
        while True:
            assert process.process.poll() is None, "T10 Vite启动失败"
            try:
                response = await client.get(url, timeout=max(.01, min(1, deadline - time.monotonic())))
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(.05)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("visual",), required=True)
    parser.parse_args()
    from tests.e2e.costing_quote_lifecycle import run_supervised

    result = asyncio.run(run_supervised("visual"))
    print(result.output, flush=True)
    raise SystemExit(result.code)
