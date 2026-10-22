"""真实 PG/MinIO 冷启动恢复装配，邮箱与模型不得调用。"""
import asyncio

from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
from apps.scheduler_worker.standalone import create_standalone_factory
from tests.integration.test_pilot_persistence import initialized
from tests.integration.test_pilot_persistence import (
    owned_profiles as owned_profiles,  # noqa: PLC0414
)
from tests.integration.test_standalone_reply_runtime import NoModel
from tests.unit.test_qq_reply_runtime import qq_settings


def test_recovery_cold_boot_keeps_reply_workflow_without_mail_scan(owned_profiles):
    directory, profiles = owned_profiles
    owned = initialized(directory, profiles)
    config = qq_settings(directory)
    config = config.model_copy(update={
        "qq_reply": config.qq_reply.model_copy(update={"recovery_only": True})
    })
    factory = create_standalone_factory(
        owned.config, config, NoModel(), provider_factory=NoModel().provider
    )

    async def exercise():
        async with factory() as runtime:
            assert runtime.inbound_driver is None
            assert runtime.assistant_driver is not None
            stop = asyncio.Event()

            async def one_cycle(interval, event):
                stop.set()

            result = await run_scheduler_worker(
                runtime, stop_event=stop, wait=one_cycle, install_signal_handlers=False
            )
            assert result.status == WorkerStartStatus.STARTED
            assert result.cycles_completed == 1

    asyncio.run(exercise())
