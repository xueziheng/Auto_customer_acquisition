"""进程装配事实；心跳不是模型探测，也不解析凭证。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId, new_id


class ModelRuntimeLifecycle:
    heartbeat_interval_seconds = 10

    def __init__(
        self,
        repository: SqlModelConfigurationRepository,
        settings: StandaloneModelSettings,
        tenant_id: TenantId,
        process: Literal["api", "scheduler"],
        recovery: Callable[[str], Awaitable[int]] | None = None,
        instance_id: str | None = None,
    ) -> None:
        self._repository, self._settings, self._tenant_id, self._process = (
            repository,
            settings,
            tenant_id,
            process,
        )
        self._recovery = recovery
        self._instance_id = instance_id or new_id("mrt").lower()
        self._task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def startup(self) -> None:
        config = self._settings
        await self._repository.initialize(
            self._tenant_id,
            config.configuration_version,
            config.model,
            config.limits,
            config.model_data_export_enabled,
        )
        await self._repository.register_process(
            self._tenant_id,
            self._process,
            config.configuration_version,
            self._instance_id,
        )
        self._started = True
        if self._process == "api":
            await self.start_heartbeat(None)

    async def heartbeat(self) -> None:
        if self._recovery is not None:
            await self._recovery(self._instance_id)
        await self._repository.heartbeat(
            self._tenant_id, self._process, self._instance_id
        )

    async def start_heartbeat(
        self, guard: Callable[[], Awaitable[None]] | None
    ) -> None:
        if self._process == "scheduler" and guard is None:
            raise ValueError("后台心跳需要单副本锁核验")
        if self._task is None:
            self._task = asyncio.create_task(self._heartbeat_loop(guard))

    async def _heartbeat_loop(
        self, guard: Callable[[], Awaitable[None]] | None
    ) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            try:
                if guard is not None:
                    await guard()
                # 崩溃恢复只由持锁 driver 扫描；并行心跳不执行恢复或业务动作。
                await self._repository.heartbeat(
                    self._tenant_id, self._process, self._instance_id
                )
            except Exception:  # noqa: BLE001 - 过期即拒绝模型动作，禁止记录连接详情
                return

    async def stop_heartbeat(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def aclose(self) -> None:
        await self.stop_heartbeat()
        if self._started:
            await self._repository.unregister_process(
                self._tenant_id, self._process, self._instance_id
            )
            self._started = False
