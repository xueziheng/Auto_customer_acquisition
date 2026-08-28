"""固定Linux runner内部用例；fault入口只存在于测试包。"""

from __future__ import annotations

import os
import struct
import sys
import time


def _fault_main(action):
    from connectors.evidence_text.worker import (
        emit,
        read_control,
        read_exact,
        set_resources,
    )

    control = read_control()
    limits = control["limits"]
    set_resources(limits["cpu_seconds"], limits["address_space_bytes"])
    read_exact(sys.stdin.buffer, control["input_bytes"])
    if action == "cpu":
        while True:
            pass
    if action == "as":
        allocated = []
        try:
            for _ in range(96):
                value = bytearray(1048576)
                for index in range(0, len(value), 4096):
                    value[index] = 1
                allocated.append(value)
        except MemoryError:
            del allocated
            emit({"error": "parse_limit_exceeded"}, limits["maximum_result_bytes"])
            return
        raise SystemExit(72)
    if action == "ipc":
        sys.stdout.buffer.write(struct.pack("!I", limits["maximum_result_bytes"] + 1))
        sys.stdout.buffer.flush()
    if action in {"ipc", "wall"}:
        while True:
            time.sleep(1)
    if action == "short":
        sys.stdout.buffer.write(struct.pack("!I", 20) + b"{}")
        return
    payload = {
        "protocol": "evidence-worker-v1",
        "profile": control["profile"],
        "page": control["page"],
        "text": "controlled",
    }
    if action == "unknown":
        payload["unknown"] = True
    if action == "profile":
        payload["profile"] = "pdf-text-v1"
        payload["page"] = 1
    if action == "env":
        if set(os.environ) - {"LANG", "LC_ALL"}:
            raise SystemExit(73)
        if sys.argv[1:] != ["env"]:
            raise SystemExit(74)
    emit(payload, limits["maximum_result_bytes"])


if __name__ == "__main__":
    _fault_main(sys.argv[1])
    raise SystemExit(0)

import asyncio

import pytest

from connectors.evidence_text.client import LinuxEvidenceTextParser
from shared.schemas.evidence_read import QuoteEvidenceError
from tests.unit.test_evidence_parser_client import probe_limits
from tests.unit.test_evidence_text_profiles import parse_limits, pdf_bytes

EMAIL = b"Content-Type: text/plain; charset=utf-8\r\n\r\nWe need 50 pieces.\r\n"


def parser(**changes):
    return LinuxEvidenceTextParser(
        limits=parse_limits(**changes), probe_limits=probe_limits()
    )


def fault(monkeypatch, action):
    original = asyncio.create_subprocess_exec
    pids = []

    async def spawn(*args, **kwargs):
        if args[3] == "connectors.evidence_text.worker":
            args = (*args[:3], "tests.integration.evidence_parser_linux_cases", action)
        child = await original(*args, **kwargs)
        pids.append(child.pid)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return pids


def assert_reaped(pids):
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


async def test_real_probe_then_pdf_and_rfc822():
    instance = parser()
    assert instance.capability().status == "unavailable"
    result = await instance.probe()
    assert result.status == "available", result.failure
    assert (
        await instance.parse(pdf_bytes(), profile="pdf-text-v1", page=1)
    ).text == "Controlled supplier statement\n"
    assert (
        await instance.parse(EMAIL, profile="rfc822-plain-v1", page=None)
    ).text == "We need 50 pieces.\n"
    await instance.aclose()


@pytest.mark.parametrize(
    "action,changes,code",
    [
        ("cpu", {"cpu_seconds": 1}, "parse_limit_exceeded"),
        ("as", {"address_space_bytes": 67108864}, "parse_limit_exceeded"),
        ("wall", {"wall_timeout_ms": 100}, "parse_timeout"),
        ("ipc", {}, "parse_limit_exceeded"),
        ("short", {}, "parse_unavailable"),
        ("unknown", {}, "parse_unavailable"),
        ("profile", {}, "parse_unavailable"),
    ],
)
async def test_same_resource_functions_and_reaping(monkeypatch, action, changes, code):
    instance = parser(**changes)
    assert (await instance.probe()).status == "available"
    with monkeypatch.context() as patch:
        pids = fault(patch, action)
        with pytest.raises(QuoteEvidenceError) as caught:
            await instance.parse(EMAIL, profile="rfc822-plain-v1", page=None)
        assert caught.value.code == code
        assert_reaped(pids)
    if instance.capability().status != "available":
        assert (await instance.probe()).status == "available"
    # 目标wall预算只用于故障，不把100ms当合法解析baseline。
    if action != "wall":
        assert (
            await instance.parse(EMAIL, profile="rfc822-plain-v1", page=None)
        ).text == "We need 50 pieces.\n"
    await instance.aclose()


async def test_child_env_and_argv_are_clean(monkeypatch):
    instance = parser()
    assert (await instance.probe()).status == "available"
    monkeypatch.setenv("TEST_DATABASE_URL", "controlled-sentinel-not-inherited")
    pids = fault(monkeypatch, "env")
    assert (
        await instance.parse(EMAIL, profile="rfc822-plain-v1", page=None)
    ).text == "controlled"
    assert_reaped(pids)
    await instance.aclose()


async def test_concurrency_queue_cancel_close(monkeypatch):
    instance = parser(maximum_concurrency=2, queue_timeout_ms=100)
    assert (await instance.probe()).status == "available"
    pids = fault(monkeypatch, "wall")
    calls = [
        asyncio.create_task(instance.parse(EMAIL, profile="rfc822-plain-v1", page=None))
        for _ in range(2)
    ]
    deadline = asyncio.get_running_loop().time() + 2
    while len(pids) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert len(pids) == 2
    with pytest.raises(QuoteEvidenceError) as caught:
        await instance.parse(EMAIL, profile="rfc822-plain-v1", page=None)
    assert caught.value.code == "parse_timeout" and len(pids) == 2
    calls[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await calls[0]
    await instance.aclose()
    with pytest.raises(asyncio.CancelledError):
        await calls[1]
    assert_reaped(pids)


async def test_failed_probe_never_keeps_ready(monkeypatch):
    instance = parser()
    assert (await instance.probe()).status == "available"
    monkeypatch.setattr(
        "connectors.evidence_text.client.platform.system", lambda: "Darwin"
    )
    assert (await instance.probe()).status == "unavailable"
    with pytest.raises(QuoteEvidenceError) as caught:
        await instance.parse(EMAIL, profile="rfc822-plain-v1", page=None)
    assert caught.value.code == "parse_unavailable"


async def test_runtime_exact_patch_and_dependency_gate(monkeypatch):
    from connectors.evidence_text import client

    for target, replacement in [("version", lambda name: "6.16.1")]:
        with monkeypatch.context() as patch:
            patch.setattr(client.importlib.metadata, target, replacement)
            assert (await parser().probe()).failure == "runtime"
    with monkeypatch.context() as patch:
        patch.setattr(client.platform, "python_implementation", lambda: "PyPy")
        assert (await parser().probe()).failure == "runtime"
    with monkeypatch.context() as patch:
        patch.setattr(client.sys, "version_info", (3, 12, 13))
        assert (await parser().probe()).failure == "runtime"


async def test_finite_probe_budget_cannot_fake_as_proof():
    instance = LinuxEvidenceTextParser(
        limits=parse_limits(), probe_limits=probe_limits(maximum_probe_bytes=1048576)
    )
    result = await instance.probe()
    assert result.status == "unavailable" and result.failure == "resource"


async def test_json_escape_result_limit_and_invalid_encoding():
    instance = parser(maximum_result_bytes=200)
    assert (await instance.probe()).status == "available"
    with pytest.raises(QuoteEvidenceError) as caught:
        await instance.parse(
            b"Content-Type: text/plain; charset=utf-8\n\n" + ("汉" * 40).encode(),
            profile="rfc822-plain-v1",
            page=None,
        )
    assert caught.value.code == "parse_limit_exceeded"
    with pytest.raises(QuoteEvidenceError) as caught:
        await instance.parse(
            b"Content-Type: text/plain; charset=utf-8\n\n\xff",
            profile="rfc822-plain-v1",
            page=None,
        )
    assert caught.value.code == "parse_unsupported"
    await instance.aclose()


async def test_production_worker_rejects_fault_mode():
    instance = parser()
    header = {
        "protocol": "evidence-worker-v1",
        "operation": "parse",
        "profile": "rfc822-plain-v1",
        "page": None,
        "input_bytes": len(EMAIL),
        "limits": parse_limits().model_dump(),
        "fault": "cpu",
    }
    code, _, _ = await instance._run(
        "connectors.evidence_text.worker", header, EMAIL, 2097152, 8000, 200
    )
    assert code == 70
    await instance.aclose()


async def test_cancel_probe_reaps_and_cannot_cache_success():
    instance = parser()
    call = asyncio.create_task(instance.probe())
    deadline = asyncio.get_running_loop().time() + 2
    while not instance._processes and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    pids = [process.pid for process in instance._processes]
    assert pids
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert instance.capability().status == "unavailable"
    assert_reaped(pids)


async def test_aclose_during_probe_never_enables():
    instance = parser()
    call = asyncio.create_task(instance.probe())
    deadline = asyncio.get_running_loop().time() + 2
    while not instance._processes and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    pids = [process.pid for process in instance._processes]
    await instance.aclose()
    assert (await call).status == "unavailable"
    assert_reaped(pids)


async def test_cancel_queued_reprobe_cannot_enable_older_probe():
    instance = parser()
    first = asyncio.create_task(instance.probe())
    deadline = asyncio.get_running_loop().time() + 2
    while not instance._processes and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    second = asyncio.create_task(instance.probe())
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert (await first).status == "unavailable"
    assert instance.capability().status == "unavailable"
    await instance.aclose()
