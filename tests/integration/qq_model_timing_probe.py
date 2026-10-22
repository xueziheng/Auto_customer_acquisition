"""只用于隔离测试的固定阶段时间线；不记录参数、SQL、正文或异常原文。"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from infra.db.model_usage import SqlModelUsageRepository
from infra.db.repositories.tool_calls import ToolCallRepositoryImpl
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from tool_gateway import qq_trial_model
from tool_gateway.checks.model import ModelIdentityCheck, ModelQuotaCheck
from tool_gateway.handlers.model_generate import ModelGenerateHandler
from tool_gateway.pipeline import ToolGateway


class TimingProbe:
    def __init__(self, *, block=None, clock_kind="wall_clock"):
        assert clock_kind in {"wall_clock", "controlled_expiry"}
        self.start = asyncio.get_running_loop().time()
        self.events = []
        self.block = block
        self.block_reached = False
        self.blocked = asyncio.Event()
        self.clock_kind = clock_kind
        self.active_timer = None
        self.expiry_injected = False
        self.timeouts = []
        self.units = 0

    def mark(self, stage, **safe):
        self.events.append(
            {
                "ms": round((asyncio.get_running_loop().time() - self.start) * 1000, 3),
                "stage": stage,
                **safe,
            }
        )

    async def hold(self, stage):
        self.mark(stage)
        if self.block == stage:
            self.block_reached = True
            self.blocked.set()
            self.mark("injected_wait.enter")
            try:
                await asyncio.Event().wait()
            finally:
                self.mark("injected_wait.exit")

    @asynccontextmanager
    async def timeout(self, seconds):
        self.timeouts.append(seconds)
        # 确定性场景从入口起使用无墙钟的可控 deadline；真实一秒另行独立验收。
        created_before = asyncio.get_running_loop().time()
        timer = asyncio.timeout(seconds if self.clock_kind == "wall_clock" else None)
        created_after = asyncio.get_running_loop().time()
        self.active_timer = timer
        self.mark(
            "deadline.enter",
            configured_seconds=seconds,
            clock_kind=self.clock_kind,
            created_before_ms=round((created_before - self.start) * 1000, 3),
            created_after_ms=round((created_after - self.start) * 1000, 3),
            deadline_ms=(
                round((timer.when() - self.start) * 1000, 3)
                if timer.when() is not None
                else None
            ),
        )
        try:
            async with timer:
                yield timer
        finally:
            self.mark("deadline.exit", expired=timer.expired())
            self.active_timer = None

    def expire(self):
        """测试调度器只注入一次到期；使用原 asyncio.Timeout 的取消与转换语义。"""
        assert self.clock_kind == "controlled_expiry"
        assert self.active_timer is not None and not self.expiry_injected
        assert self.active_timer.when() is None
        self.expiry_injected = True
        self.mark("controlled_expiry.injected")
        self.active_timer.reschedule(asyncio.get_running_loop().time())

    def install(self, monkeypatch):
        # 只替换被测模块的命名空间；全局 asyncio 和网关五秒清理完全不动。
        monkeypatch.setattr(
            qq_trial_model,
            "asyncio",
            SimpleNamespace(timeout=self.timeout, shield=asyncio.shield),
        )
        probe = self

        def wrap(cls, name, label, *, after=None):
            original = getattr(cls, name)

            async def traced(instance, *args, **kwargs):
                tag = label + (":" + instance.name if cls is ModelIdentityCheck else "")
                probe.mark(tag + ".enter")
                try:
                    result = await original(instance, *args, **kwargs)
                except BaseException:
                    probe.mark(tag + ".interrupted")
                    raise
                probe.mark(tag + ".returned")
                if cls is ToolCallRepositoryImpl:
                    getattr(instance, "_probe_steps", []).append(name)
                if after:
                    await probe.hold(after)
                return result

            monkeypatch.setattr(cls, name, traced)

        for name in (
            "create_received",
            "claim",
            "mark_executing",
            "complete",
            "append_event",
        ):
            wrap(ToolCallRepositoryImpl, name, "ledger." + name)
        for name in ("reserve", "mark_dispatched", "finish"):
            wrap(
                SqlModelUsageRepository,
                name,
                "usage." + name,
                after="after_quota_commit" if name == "reserve" else None,
            )
        wrap(ModelIdentityCheck, "check", "identity")
        wrap(ModelQuotaCheck, "check", "quota")
        wrap(ModelGenerateHandler, "prepare", "handler.prepare")
        wrap(ModelGenerateHandler, "execute", "handler.execute")
        wrap(ToolGateway, "_execute_claimed", "gateway.execute_claimed")
        wrap(ToolGateway, "_settle_cancelled", "gateway.cancel_cleanup")

        enter = SqlAlchemyToolGatewayUnitOfWork.__aenter__
        leave = SqlAlchemyToolGatewayUnitOfWork.__aexit__

        async def traced_enter(unit):
            result = await enter(unit)
            probe.units += 1
            unit._probe_number = probe.units
            unit.calls._probe_steps = []
            probe.mark("transaction.enter", unit=unit._probe_number)
            return result

        async def traced_leave(unit, exc_type, exc, tb):
            steps = list(unit.calls._probe_steps)
            probe.mark("transaction.exit.enter", unit=unit._probe_number, steps=steps)
            try:
                await leave(unit, exc_type, exc, tb)
            except BaseException:
                probe.mark("transaction.exit.unconfirmed", unit=unit._probe_number)
                raise
            probe.mark(
                "transaction.committed"
                if exc_type is None
                else "transaction.rolled_back",
                unit=unit._probe_number,
                steps=steps,
            )
            if exc_type is None:
                for operation, stage in (
                    ("create_received", "after_received_commit"),
                    ("claim", "after_claim_commit"),
                    ("mark_executing", "after_executing_commit"),
                ):
                    if operation in steps:
                        await probe.hold(stage)

        monkeypatch.setattr(SqlAlchemyToolGatewayUnitOfWork, "__aenter__", traced_enter)
        monkeypatch.setattr(SqlAlchemyToolGatewayUnitOfWork, "__aexit__", traced_leave)

    async def capture(self, name, gate, sessions, sdk, *, outcome, reached):
        from tests.integration.test_qq_trial_model_boundaries import ledgers

        report = {
            "case": name,
            "outcome": outcome,
            "reached": reached,
            "configured_timeouts": self.timeouts,
            "clock_kind": self.clock_kind,
            "expiry_injected": self.expiry_injected,
            "sdk_calls": len(sdk.requests),
            "block_reached": self.block_reached,
            "timeline": list(self.events),
        }
        try:
            async with asyncio.timeout(10):
                report["ledger"] = await ledgers(gate, sessions)
        except BaseException:  # noqa: BLE001 -- 失败证据只记录不可用，不泄露异常原文
            report["snapshot_unavailable"] = True
        directory = os.environ.get("QQ_TIMING_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / (name + ".json")
            assert not path.exists(), "证据文件不可覆盖"
            path.write_text(
                json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8"
            )
        print(json.dumps({k: v for k, v in report.items() if k != "timeline"}))
        return report
