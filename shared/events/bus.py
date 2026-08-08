"""事件总线 —— 域间通信的唯一通道。

为什么需要它：``domains/a`` 不得 ``import domains.b``（见
``docs/architecture/02-boundaries.md``）。这是最容易被违反、也最值钱的
一条规则——一旦域间直接依赖，改一个需求要动七个文件，而那种痛苦不是
因为业务复杂，是因为边界烂了。

事件是首选的跨域协作方式。只在"必须立刻拿到返回值才能继续"时才用
对方 ``service.py`` 显式导出的接口（例如报价前必须先读到成本）。

判断方法：
    只是通知"某件事发生了"，对方自己决定要不要反应   → 事件
    需要立刻拿到结果才能继续                        → 服务接口

默认选事件。同步调用会把两个域的可用性绑在一起。
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from shared.events.catalog import DomainEvent

E_contra = TypeVar("E_contra", bound=DomainEvent, contravariant=True)


@runtime_checkable
class EventHandler(Protocol[E_contra]):
    """事件处理器。

    实现约定：
    - **必须幂等。** 同一事件可能被投递多次（重试、Worker 重启后重扫）。
    - 处理失败要抛异常，由总线决定重试策略；不要吞掉异常。
    - 不要在处理器里做长耗时操作——那属于工作流，应该发起一个
      workflow run 而不是阻塞事件处理。
    """

    async def handle(self, event: E_contra) -> None: ...


@runtime_checkable
class EventBus(Protocol):
    """事件总线接口。

    Phase 1 实现：数据库出站表（outbox）+ ``scheduler_worker`` 轮询投递。
    这样事件发布与业务写入在同一事务里，不会出现"业务改了但事件丢了"。

    **不要**在 Phase 1 用纯内存总线：进程重启会丢事件，而丢一个
    ``NeedValidated`` 意味着一条已验证需求永远不会进入机会评估。
    """

    async def publish(self, event: DomainEvent) -> None:
        """发布事件。

        实现要求：
        - 与调用方的业务写入**在同一事务内**落到 outbox 表，
          保证原子性（要么业务改了事件也在，要么都没发生）
        - 携带 ``tenant_id``、``run_id``、``occurred_at``
        - 投递本身是异步的，``publish`` 返回不代表订阅方已处理完
        """
        ...

    async def publish_many(self, events: list[DomainEvent]) -> None:
        """批量发布。同一事务内的多个事件应该用这个，减少往返。"""
        ...

    def subscribe(
        self, event_type: type[E_contra], handler: EventHandler[E_contra]
    ) -> None:
        """注册订阅。

        订阅关系在应用启动时装配（见 ``apps/api`` 与各 worker 的入口），
        不在域内部硬编码——域不应该知道谁在听它的事件。
        """
        ...


class EventEnvelope:
    """投递信封。

    除事件本身外携带投递元数据：

    ```text
    event_id        事件唯一 ID，用于订阅方幂等去重
    attempt         第几次投递
    published_at    发布时间
    trace_id        链路追踪 ID，串起 Run 与后续处理
    ```

    订阅方用 ``event_id`` 做幂等：记录已处理的 ID，重复投递直接跳过。
    """

    ...
