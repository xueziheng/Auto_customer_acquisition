"""outbox 事件投递（S3-8；``OutboxDeliverer`` 核心投递语义）。

职责范围（本实现覆盖核心投递、retry/backoff/dead-letter 与未知事件类型隔离）：

- **handler 注册表与 EVENT_REGISTRY 分工**：``EVENT_REGISTRY`` 是事件类型白名单
  （发布/反序列化共用），只表示「系统认识这个事件类型」，不表示有任何订阅方。
  handler 必须经 ``register_handler`` 显式注册（实例级注册表）；对某事件类型
  没有任何注册 handler 时，事件标记 ``dead``，``last_error`` 只写固定
  ``NO_REGISTERED_HANDLER`` + ``event_type``，绝不落 payload/异常原文。
- **租户过滤失败关闭**：扫描/更新全部显式带 ``tenant_id``（硬边界 8）。
- **FOR UPDATE SKIP LOCKED + 逐事件独立事务**：每个事件一个会话/事务领取并处理；
  一个事件失败不影响同批其余事件；并发 drain 不会重复投递同一事件。
- **durable per-handler 投递**：``outbox_deliveries`` 持久化每个 handler 的状态
  （pending/delivered/dead）；事件仅当**所有**注册 handler 均 delivered 才
  delivered。handler 成功后其 delivery 置 delivered（清空该 delivery 的
  ``next_attempt_at``/``last_error``）并随事务提交——后续 drain 只继续未完成
  （pending）的 handler，不会重跑已 delivered 的 handler（durable per-handler
  resume）；事件全部 delivered 时清空事件行的 ``next_attempt_at``/``last_error``。
  永久失败 → 该 delivery 与事件进 ``dead``（事件 ``next_attempt_at`` 置 NULL、
  ``last_error`` 写脱敏值）；``TransientError`` → 该 delivery 递增 attempts 并
  记录正退避（``next_attempt_at``，确定性且随 attempts 增长），事件行同步 pending
  delivery 聚合（``next_attempt_at``/脱敏 ``last_error``）；``attempts >=
  max_attempts`` 耗尽 → delivery 与事件进 ``dead``；未耗尽保持 pending，后续
  drain 到期后续投。
- **handler 增删与 durable delivery 行一致**：事件终态判定考虑该事件**所有**
  durable delivery 行（不只当前注册 handler 的行）。新注册 handler 在非终态事件
  上会创建其 durable delivery 行并纳入终态判定；handler 在其 delivery 仍 pending
  时被移除 → 事件进 ``dead``（固定 ``HANDLER_REMOVED_WHILE_PENDING`` + event_type，
  结构化日志不含 payload/异常文本）——残留 pending 行不会让事件被反复领取
  （tight loop），也不会被其余 handler 掩盖成 delivered。
- **到期过滤**：drain 领取按事件所有 pending delivery 的 ``next_attempt_at`` 做
  到期过滤——存在「未到期 pending delivery」（``next_attempt_at`` 在未来）的事件
  不可被领取（未到点不重试，不毒化 tight loop）。
- **未知事件类型隔离**：事件类型不在 ``EVENT_REGISTRY`` 白名单（``resolve_event_type``
  抛 ``ValidationError``）时，该事件保持 ``pending``（不标记 delivered），回滚其
  事务、记录结构化错误（不含 payload/异常原文）后继续处理同批其余事件（坏事件
  不饿死同批）；drain 处理完本批后统一重新抛出原 ``ValidationError``（保留 registry
  失败语义，不静默吞掉）。
- **已知类型 payload 反序列化失败隔离**：事件类型在 ``EVENT_REGISTRY`` 白名单内、
  且已有注册 handler，但 ``deserialize`` 抛异常（payload 有损）→ 该事件 ``dead``
  （固定 ``PAYLOAD_DESERIALIZATION_FAILED`` + event_type，``next_attempt_at`` 置
  NULL），结构化日志不含 payload/异常文本；与未知类型（保持 pending）区分，不
  产生 pending 紧循环。
- **DB 异常迭代内隔离**：drain 循环内任何 execute/flush/commit 异常 → 回滚该事件
  事务，记录固定结构化日志（仅 event_id/tenant_id/event_type，不含 payload/异常
  文本），继续处理同批其余事件；失败事件保持 ``pending``、无退避，下次 drain 可重试。
  选中任何事件前就遭遇 DB 故障（首个 SELECT 失败，``event_id`` 为空）→ 无 event
  可推进循环，fail closed 终止当前 drain cycle（同样只记录一次固定结构化日志，
  不含 payload/异常/DSN），未处理事件保持 pending，下次 drain 再试。
- **错误脱敏**：持久化的 ``last_error`` 只写异常类型名（``type(exc).__name__``），
  不写异常消息/payload/凭证文本；日志消息用固定文本，结构化上下文走 ``extra``。
- **at-least-once 崩溃边界（诚实声明）**：**只有数据库状态变更（delivery/event
  状态行）在单个事务内原子提交**；handler 的外部副作用不参与该事务，无法与它
  原子提交——「投递动作与状态一起原子提交」在外部副作用存在时不可能成立。
  handler 执行成功后、delivery 标记 delivered 并 commit 之间若进程崩溃，该事件
  仍为 ``pending``，重启后会被重新投递、handler 再次被调用，外部副作用可能重复
  执行。因此语义是 **at-least-once**，handler 必须幂等（``EventHandler`` Protocol
  已要求）并在重复投递时自行去重。已提交 delivered 的 handler 不会在后续 drain
  中被重跑（resume 只处理 pending delivery）。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.outbox import deserialize, resolve_event_type
from infra.db.tables import OutboxDeliveryRow, OutboxEventRow
from shared.errors import TransientError, ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import DomainEvent
from shared.schemas.identifiers import TenantId, new_id

logger = logging.getLogger(__name__)

# 固定脱敏错误标记：事件无注册 handler 时 last_error 的前缀（只带 event_type，
# 不含 payload；见测试要求「persist only fixed NO_REGISTERED_HANDLER + event_type」）。
NO_REGISTERED_HANDLER = "NO_REGISTERED_HANDLER"

# 固定脱敏错误标记：handler 在其 delivery 仍 pending 时被移除 → 事件 dead 时
# last_error 的前缀（只带 event_type，不含 payload/异常文本）。
HANDLER_REMOVED_WHILE_PENDING = "HANDLER_REMOVED_WHILE_PENDING"

# 固定脱敏错误标记：已知事件类型（EVENT_REGISTRY 白名单内）且已有注册 handler，
# 但其 payload 无法反序列化 → 事件 dead 时 last_error 的前缀（只带 event_type，
# 不含 payload/异常文本）。与未知事件类型（ValidationError → 保持 pending）区分：
# 这是「系统认识但有损的负载」，不是「系统不认识的契约」。
PAYLOAD_DESERIALIZATION_FAILED = "PAYLOAD_DESERIALIZATION_FAILED"

# TransientError 耗尽（attempts >= max_attempts）转死信时的固定脱敏错误文本。
_RETRY_EXHAUSTED = "retry exhausted"

# 默认重试上限：未显式传入 max_attempts 时 TransientError 耗尽该次数后转 dead。
_DEFAULT_MAX_ATTEMPTS = 3


class OutboxDeliverer:
    """outbox 事件投递器（Postgres）。

    构造不触数据库。``register_handler`` 显式注册订阅（handler 注册表，与
    ``EVENT_REGISTRY`` 白名单分工：注册 handler 不代表事件类型进白名单，
    事件类型在白名单也不代表有订阅）。``drain`` 扫描本租户 ``pending`` 事件
    并投递，逐事件独立事务，领取时按 delivery 的 ``next_attempt_at`` 到期过滤。
    ``now``/``max_attempts`` 可注入以便测试确定化；``max_attempts`` 必须为正整数
    （TransientError 耗尽该次数后 delivery/event 转 dead），未传入用默认值。
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
        max_attempts: int | None = None,
    ) -> None:
        if max_attempts is not None and (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValidationError("max_attempts 必须是正整数（>= 1）")
        self._factory = factory
        self._tenant_id = tenant_id
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._max_attempts = (
            max_attempts if max_attempts is not None else _DEFAULT_MAX_ATTEMPTS
        )
        # handler 注册表：event_type 名 → [(handler_name, handler)]。与 EVENT_REGISTRY
        # 分工：这里只表达「谁订阅了这个类型」，不校验类型是否在白名单。
        self._handlers: dict[str, list[tuple[str, EventHandler[DomainEvent]]]] = {}

    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None:
        """显式注册 handler（实例级注册表）；同一事件类型可注册多个 handler。

        同一 ``event_type`` 下 ``handler_name`` 必须唯一——重复注册是装配错误
        （两个 handler 会映射到同一个 delivery 行，投递语义变模糊），fail closed
        抛 ``ValidationError``，不静默接受。不同 handler_name 的多个 handler、
        不同 event_type 下的同名 handler 均合法。
        """
        registered = self._handlers.setdefault(event_type.__name__, [])
        if any(name == handler_name for name, _ in registered):
            raise ValidationError(
                f"handler {handler_name} 已在事件类型 {event_type.__name__} 注册"
            )
        registered.append((handler_name, handler))

    async def drain(self) -> int:
        """扫描并投递本租户所有 ``pending`` 事件，返回处理的事件数。

        逐事件独立事务：``FOR UPDATE SKIP LOCKED`` 领取；handler 成功 →
        delivery delivered，永久失败 → delivery/event 进 ``dead``，
        ``TransientError`` → delivery 递增 attempts：``attempts >= max_attempts``
        耗尽 → delivery/event 进 ``dead``，否则保持 pending 并记录正退避
        （均本事务提交）。领取按到期过滤：事件只有在没有任何「未到期 pending
        delivery」（``next_attempt_at`` 在未来）时才可被领取——未到点不重选，
        不毒化 tight loop。commit/flush/DB 级异常 → 回滚该事件事务、记录固定
        结构化日志（仅 event_id/tenant_id/event_type，不含 payload/异常文本）
        并继续本批其余事件（坏事件不饿死同批）；失败事件保持 pending，下次
        drain 可重试。选中任何事件前就遭遇 DB 级故障（首个 SELECT 失败，
        event_id 为空）→ 无 event 可推进循环，回滚、记录一次固定结构化日志后
        fail closed 终止当前 drain cycle（不无限重试同一故障）；未处理事件保持
        pending，下次 drain 再试。事件类型不在白名单（未知类型，
        ``resolve_event_type`` 抛 ``ValidationError``）→ 该事件保持 pending、回滚
        其事务、记录结构化错误（不含 payload/异常原文），继续处理同批其余事件；
        本批处理完后统一重新抛出原 ``ValidationError``（保留 registry 失败语义，
        不静默吞掉、不标记 delivered）。已处理过的 event_id 记入 ``tried`` 集合，
        同批内不重复领取（防 DB 级失败造成的 tight loop）。
        """
        now = self._now()
        processed = 0
        tried_event_ids: set[str] = set()
        # 收集未知/非法事件类型触发的 ValidationError：本批处理完后统一重新抛出
        # （保留 registry 失败语义），同时不影响同批其余事件继续处理。
        collected_errors: list[ValidationError] = []
        while True:
            session = self._factory()
            event_row: OutboxEventRow | None = None
            # rollback 会使 ORM 实例过期，except 路径不得再访问实例属性（会触发
            # 异步延迟刷新 → MissingGreenlet）；领取后先捕获纯标量供日志使用。
            event_id = ""
            event_tenant = ""
            event_type = ""
            try:
                event_row = (
                    await session.execute(
                        select(OutboxEventRow)
                        .where(
                            OutboxEventRow.tenant_id == self._tenant_id,
                            OutboxEventRow.status == "pending",
                            OutboxEventRow.event_id.not_in(tried_event_ids),
                            # 到期过滤：事件只有在没有任何「未到期 pending delivery」
                            # （next_attempt_at 在未来）时才可领取。next_attempt_at 为
                            # NULL（崩溃残留）或已到期 → 可选；未到点不重选（无 tight
                            # loop）。子查询按 (tenant_id, event_id) 关联，租户过滤
                            # 失败关闭（硬边界 8）。
                            ~exists(
                                select(OutboxDeliveryRow.delivery_id).where(
                                    OutboxDeliveryRow.tenant_id
                                    == OutboxEventRow.tenant_id,
                                    OutboxDeliveryRow.event_id
                                    == OutboxEventRow.event_id,
                                    OutboxDeliveryRow.status == "pending",
                                    OutboxDeliveryRow.next_attempt_at.is_not(None),
                                    OutboxDeliveryRow.next_attempt_at > now,
                                )
                            ),
                        )
                        .order_by(
                            OutboxEventRow.published_at.asc(),
                            OutboxEventRow.event_id.asc(),
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().first()
                if event_row is None:
                    await session.rollback()
                    break
                tried_event_ids.add(event_row.event_id)
                event_id = event_row.event_id
                event_tenant = event_row.tenant_id
                event_type = event_row.event_type
                await self._process_event(session, event_row, now)
                await session.commit()
                processed += 1
            except ValidationError as exc:
                # 未知/非法事件类型：保留 registry 失败语义——回滚该事件事务（保持
                # pending、不标记 delivered），记录结构化错误（不含 payload/异常原文），
                # 继续处理同批其余事件（坏事件不饿死同批）。该事件已记入 tried，
                # 同批内不重复领取（无 tight loop）。本批处理完后统一重新抛出。
                await session.rollback()
                logger.error(
                    "outbox event skipped: unknown event type",
                    extra={
                        "event_id": event_id,
                        "tenant_id": event_tenant,
                        "event_type": event_type,
                    },
                )
                collected_errors.append(exc)
            except Exception:  # noqa: BLE001  commit/flush/DB 级异常：迭代内隔离
                await session.rollback()
                # 固定结构化脱敏日志：只带 event_id/tenant_id/event_type（已知时），
                # 不含 payload/异常文本（凭证不落日志）；失败事件保持 pending、
                # 无退避，下次 drain 可重试。
                logger.error(
                    "outbox event skipped: db failure",
                    extra={
                        "event_id": event_id,
                        "tenant_id": event_tenant,
                        "event_type": event_type,
                    },
                )
                if not event_id:
                    # 选中任何事件前的 DB 故障：无 event 可记入 tried_event_ids 推进
                    # 循环，继续迭代只会无限复现同一故障（tight loop）→ fail closed
                    # 终止当前 drain cycle（已记录一次固定脱敏日志；session 由 finally
                    # 关闭）。未处理事件保持 pending，下次 drain 再试。
                    break
            finally:
                await session.close()
        if collected_errors:
            raise collected_errors[0]
        return processed

    async def _process_event(
        self,
        session: AsyncSession,
        event_row: OutboxEventRow,
        now: datetime,
    ) -> None:
        """处理单个事件（在事件自己的事务内）。

        先做 registry 白名单校验（未知事件类型抛 ``ValidationError``，由 drain
        收集后统一重新抛出）。无注册 handler → 事件 ``dead``（固定
        ``NO_REGISTERED_HANDLER`` + ``event_type``，不含 payload，不产生 delivery
        行）。否则先载入该事件所有 durable delivery 行（终态判定考虑每一个持久化
        行，不只当前注册 handler 的行）：某 delivery 的 handler 已不在注册表且仍
        pending → 事件 ``dead``（固定 ``HANDLER_REMOVED_WHILE_PENDING`` +
        event_type，结构化日志不含 payload/异常文本）。随后逐 handler（含新注册
        handler——为其创建 durable delivery 行）：
        复用 delivery 行（durable，幂等续投），跳过已 delivered/dead 的
        delivery；调用 handler，成功 → delivered（清空该 delivery 的
        ``next_attempt_at``/``last_error``），``TransientError`` → 递增 attempts
        （``attempts >= max_attempts`` 耗尽 → delivery 进 dead，否则保持 pending
        记录正退避并同步事件行的 ``next_attempt_at``/脱敏 ``last_error``），其他
        异常 → dead（脱敏错误）。
        全部 delivered → 事件 delivered（清空事件行 ``next_attempt_at``/
        ``last_error``）；任一 dead → 事件 dead（``next_attempt_at`` 置 NULL、写
        脱敏 ``last_error``）；仍有 pending（未完成）handler → 事件保持 pending
        （后续 drain 到期后续投）。
        """
        event_type = event_row.event_type
        # 先做 registry 白名单校验：未知事件类型在此抛 ValidationError（保留 registry
        # 失败语义）。必须在查 handler 注册表之前校验——未知类型是系统不认识的契约，
        # 不是「没有订阅方」，否则会被误判为 NO_REGISTERED_HANDLER 而进 dead。
        event_cls = resolve_event_type(event_type)
        registered = self._handlers.get(event_type, [])
        if not registered:
            self._mark_no_handler(event_row)
            return
        try:
            event = deserialize(event_cls, event_row.event_payload)
        except Exception:  # noqa: BLE001  已知类型但 payload 无法反序列化 → 死信
            self._mark_deserialization_failed(event_row)
            return
        # 一次性载入该事件所有 durable delivery 行：事件终态判定必须考虑每一个
        # 持久化行，而不只是当前注册 handler 的行（见 remove-handler 回归——被移除
        # handler 的 pending 行仍存在，事件不得仅凭其余 handler 就 delivered）。
        existing = (
            await session.execute(
                select(OutboxDeliveryRow).where(
                    OutboxDeliveryRow.tenant_id == event_row.tenant_id,
                    OutboxDeliveryRow.event_id == event_row.event_id,
                )
            )
        ).scalars().all()
        deliveries = list(existing)
        by_name = {d.handler_name: d for d in deliveries}
        registered_names = {name for name, _ in registered}

        # handler 在其 delivery 仍 pending 时被移除：该 pending 行永远无法经注册表
        # 完成，事件若保持 pending 会被反复领取（tight loop），也不能凭剩余 handler
        # 掩盖成 delivered → 进 dead（固定脱敏标记 + 结构化日志，不含 payload/异常
        # 文本），不再被轮询。
        for name, existing_delivery in by_name.items():
            if name not in registered_names and existing_delivery.status == "pending":
                self._mark_removed_handler(event_row, name)
                return

        any_dead = any(d.status == "dead" for d in deliveries)
        # 事件级 dead 结局的脱敏 last_error：同步任一 dead delivery 的脱敏 last_error
        # （异常类型名 / 固定 _RETRY_EXHAUSTED，均不含 payload/原始异常文本）。
        event_last_error: str | None = None
        for handler_name, handler in registered:
            delivery: OutboxDeliveryRow | None = by_name.get(handler_name)
            if delivery is None:
                # 新注册 handler：为该非终态事件创建 durable delivery 行（后续
                # drain 续投时与既有 handler 一样被持久化追踪）。
                delivery = await self._get_or_create_delivery(
                    session, event_row, handler_name
                )
                deliveries.append(delivery)
            if delivery.status == "delivered":
                continue  # 已投递（crash/restart 续投不重复投递）
            if delivery.status == "dead":
                any_dead = True
                if event_last_error is None and delivery.last_error is not None:
                    event_last_error = delivery.last_error
                continue  # 死信不再重试（无 tight loop）
            try:
                await handler.handle(event)
            except TransientError as exc:
                # retryable：递增 attempts；耗尽（>= max_attempts）→ delivery 进 dead
                # （脱敏固定文本），否则保持 pending 并记录正退避（到期后续投），
                # 事件行同步该 pending delivery 聚合（next_attempt_at/脱敏 last_error）。
                delivery.attempts += 1
                if delivery.attempts >= self._max_attempts:
                    delivery.status = "dead"
                    delivery.next_attempt_at = None
                    delivery.last_error = _RETRY_EXHAUSTED
                    any_dead = True
                    event_last_error = _RETRY_EXHAUSTED
                    self._log_event_dead(
                        "outbox event dead: retry exhausted",
                        event_row,
                        handler_name=handler_name,
                    )
                else:
                    delivery.next_attempt_at = self._backoff_after(
                        now, delivery.attempts
                    )
                    delivery.last_error = self._safe_error(exc)
                    event_row.next_attempt_at = delivery.next_attempt_at
                    event_row.last_error = delivery.last_error
            except Exception as exc:  # noqa: BLE001
                # 永久错误：delivery 与事件进 dead（脱敏错误，不落消息/payload）。
                delivery.status = "dead"
                delivery.attempts += 1
                delivery.next_attempt_at = None
                delivery.last_error = self._safe_error(exc)
                any_dead = True
                event_last_error = delivery.last_error
                self._log_event_dead(
                    "outbox event dead: permanent handler failure",
                    event_row,
                    handler_name=handler_name,
                )
            else:
                delivery.status = "delivered"
                delivery.delivered_at = now
                # 投递成功：清空该 delivery 的重试调度与错误痕迹（不残留 backoff/死信标记）。
                delivery.next_attempt_at = None
                delivery.last_error = None
        # 事件终态：任一 delivery dead → 事件 dead（next_attempt_at 置 NULL、写脱敏
        # last_error）；所有 delivery 均 delivered → 事件 delivered（清空事件行
        # next_attempt_at/last_error）；否则仍有 pending（未完成）handler → 事件保持
        # pending，由后续 drain 续投（TransientError 退避中不算 delivered）。终态判定
        # 覆盖全部 durable delivery 行（含被移除 handler 的 delivered/dead 残留）。
        if any_dead:
            event_row.status = "dead"
            event_row.next_attempt_at = None
            event_row.last_error = event_last_error
        elif all(d.status == "delivered" for d in deliveries):
            event_row.status = "delivered"
            event_row.delivered_at = now
            event_row.next_attempt_at = None
            event_row.last_error = None

    def _log_event_dead(
        self,
        message: str,
        event_row: OutboxEventRow,
        *,
        handler_name: str,
    ) -> None:
        """结构化脱敏死信日志：固定消息 + 标识键（event/tenant/event_type/handler_name）。

        handler_name 是装配名（非 payload/凭证）；不写异常原文/异常消息，与既有
        脱敏纪律一致（payload 与原始异常文本绝不落日志）。
        """
        logger.error(
            message,
            extra={
                "event_id": event_row.event_id,
                "tenant_id": event_row.tenant_id,
                "event_type": event_row.event_type,
                "handler_name": handler_name,
            },
        )

    def _mark_no_handler(self, event_row: OutboxEventRow) -> None:
        """无注册 handler：事件 ``dead``，``last_error`` 只写固定标记 + event_type，
        ``next_attempt_at`` 置 NULL（不表现成待重试）。"""
        event_row.status = "dead"
        event_row.last_error = (
            f"{NO_REGISTERED_HANDLER}: event_type={event_row.event_type}"
        )
        event_row.next_attempt_at = None
        logger.error(
            "outbox event dead: no registered handler",
            extra={
                "event_id": event_row.event_id,
                "tenant_id": event_row.tenant_id,
                "event_type": event_row.event_type,
            },
        )

    def _mark_removed_handler(
        self, event_row: OutboxEventRow, handler_name: str
    ) -> None:
        """handler 在其 delivery 仍 pending 时被移除：事件 ``dead``，固定脱敏标记。

        ``last_error`` 只写固定标记 + event_type（不含 payload/异常文本）、
        ``next_attempt_at`` 置 NULL（不表现成待重试）；结构化日志走 ``extra``
        （handler_name 是装配名，非 payload/凭证），不写异常原文，与
        ``_mark_no_handler`` 同样的脱敏纪律。
        """
        event_row.status = "dead"
        event_row.last_error = (
            f"{HANDLER_REMOVED_WHILE_PENDING}: event_type={event_row.event_type}"
        )
        event_row.next_attempt_at = None
        logger.error(
            "outbox event dead: handler removed while delivery pending",
            extra={
                "event_id": event_row.event_id,
                "tenant_id": event_row.tenant_id,
                "event_type": event_row.event_type,
                "handler_name": handler_name,
            },
        )

    def _mark_deserialization_failed(self, event_row: OutboxEventRow) -> None:
        """已知事件类型（EVENT_REGISTRY 白名单内）但 payload 无法反序列化：事件 dead。

        ``last_error`` 只写固定标记 + event_type（不含 payload/异常文本），
        ``next_attempt_at`` 置 NULL（不表现成待重试）；结构化日志走 ``extra``，
        与 ``_mark_no_handler`` 同样的脱敏纪律。反序列化失败发生在创建任何
        delivery 行之前，因此该事件不会产生 durable delivery 行（无 pending 紧循环）。
        """
        event_row.status = "dead"
        event_row.last_error = (
            f"{PAYLOAD_DESERIALIZATION_FAILED}: event_type={event_row.event_type}"
        )
        event_row.next_attempt_at = None
        logger.error(
            "outbox event dead: payload deserialization failed",
            extra={
                "event_id": event_row.event_id,
                "tenant_id": event_row.tenant_id,
                "event_type": event_row.event_type,
            },
        )

    async def _get_or_create_delivery(
        self,
        session: AsyncSession,
        event_row: OutboxEventRow,
        handler_name: str,
    ) -> OutboxDeliveryRow:
        """取该 (event, handler) 的 delivery 行；不存在则创建（durable，幂等续投）。

        事件行已在本事务内被 ``FOR UPDATE`` 锁定，并发 drain 无法同时为同一事件
        建 delivery 行，因此无需 ``ON CONFLICT`` 兜底。
        """
        delivery = (
            await session.execute(
                select(OutboxDeliveryRow).where(
                    OutboxDeliveryRow.tenant_id == event_row.tenant_id,
                    OutboxDeliveryRow.event_id == event_row.event_id,
                    OutboxDeliveryRow.handler_name == handler_name,
                )
            )
        ).scalars().first()
        if delivery is not None:
            return delivery
        delivery = OutboxDeliveryRow(
            delivery_id=new_id("del"),
            tenant_id=event_row.tenant_id,
            event_id=event_row.event_id,
            handler_name=handler_name,
            status="pending",
            attempts=0,
        )
        session.add(delivery)
        return delivery

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        """脱敏错误：只留异常类型名，不落异常消息/payload（防凭证进 last_error）。"""
        return type(exc).__name__

    @staticmethod
    def _backoff_after(now: datetime, attempts: int) -> datetime:
        """TransientError 后下次可重试时间：确定性正退避，且随 attempts 严格增长。

        公式 ``base * 2^(attempts-1)``（attempts 从 1 递增）：第 1 次失败后 30s、
        第 2 次 60s、第 3 次 120s……，``delay2 > delay1 > 0`` 由
        ``test_transient_error_positive_backoff`` 覆盖。``base`` 固定 30s，保证
        确定性；不依赖异常消息/payload，天然脱敏。
        """
        base = timedelta(seconds=30)
        return now + base * (2 ** (attempts - 1))
