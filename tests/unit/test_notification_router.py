"""S3-9 通知路由 RED 测试（unit）：NotificationRouter + RoutingPolicy + NotificationDedupStore。

行为断言，不依赖实现细节：
- ``NotificationRouter`` 从 S3-9 新模块 ``notification_gateway.router`` 导入；构造时注入
  **非内存** dedup store 与 routing policy；``register_channel`` 注册渠道，``dispatch``
  经 policy 选渠道、经 store 判重后**并发**投递（URGENT 多渠道 fan-out）。
- **routing policy 拥有渠道选择**：router 不内建「优先级→渠道」映射——本切片无
  in-app/email 渠道，不写「LOW 只进 in-app」；policy 选中哪条就投哪条。
- 单渠道失败不影响其他渠道、失败在 store 中记为可重试，且路由失败以 ``TransientError``
  呈现（可重试，不阻塞主业务事务）。
- **store 写失败不中断其他渠道持久化**：``record_failure``/``record_success`` 抛错时
  继续处理其余渠道，最终只抛固定脱敏、可重试 ``TransientError``，绝不泄漏 DB/渠道
  异常文本。
- 重复 dispatch（同 dedup_key + 渠道已投递）被 store 抑制，不重复投递。
- ``StructuredLogChannel``（``notification_gateway.channels.structured_log``）用固定
  logger 与固定结构化键；只输出已清理 query/token 的相对深链；绝对外链与凭证形态内容以
  ``PolicyViolation`` 拒绝（拒绝时不落日志）；**引号包裹的 password 赋值、next_step 与
  context keys 等一切会落日志的自由文本**同样受检；绝不记录原始 secrets/payload/异常文本。

RED 前置：S3-9 生产模块（``router.py``/``dedup.py``/``channels/structured_log.py``）未建，
动态加载转行为失败（pytest.fail），非收集错误。``notification_gateway.models`` 保留的公共
模型与渠道 Protocol 可直接导入；``NotificationRouter`` 已迁出 models.py（S3-9 移除重复
stale class），一律从 router.py 导入。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from datetime import UTC, datetime

import pytest

from notification_gateway.models import (
    Notification,
    NotificationChannel,
    NotificationPriority,
)
from shared.errors import PolicyViolation, TransientError
from shared.schemas.identifiers import EmployeeId, TenantId

_NOW = datetime(2026, 8, 9, 8, 0, 0, tzinfo=UTC)

_MODULE_BY_SYMBOL = {
    "NotificationRouter": "notification_gateway.router",
    "RoutingPolicy": "notification_gateway.router",
    "NotificationDedupStore": "notification_gateway.dedup",
    "StructuredLogChannel": "notification_gateway.channels.structured_log",
}

# 结构化日志渠道的固定 logger 名（模块 logger）。
_CHANNEL_LOGGER = "notification_gateway.channels.structured_log"


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED，非收集错误）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _notification(
    *,
    priority: NotificationPriority = NotificationPriority.URGENT,
    title: str = "高意向客户需接管",
    context: dict[str, str] | None = None,
    dedup_key: str = "opp-1:handoff",
    next_step: str | None = None,
    link: str | None = None,
    source_event: str = "HandoffRequested",
) -> Notification:
    """最小合法通知（URGENT 默认；字段与 models.Notification 对齐）。"""
    return Notification(
        tenant_id=TenantId("t1"),
        recipient=EmployeeId("emp-1"),
        priority=priority,
        title=title,
        context=context if context is not None else {"客户": "Acme", "国家": "US"},
        source_event=source_event,
        dedup_key=dedup_key,
        next_step=next_step if next_step is not None else "立即接管",
        due_at=_NOW,
        link=link,
    )


# --- 测试替身 ------------------------------------------------------------------


class _FakeChannel:
    """NotificationChannel 替身：记录投递的 dedup_key；可选抛错。"""

    def __init__(self, name: str, *, error: BaseException | None = None) -> None:
        self.name = name
        self.calls: list[str] = []
        self._error = error

    async def deliver(self, notification: Notification) -> None:
        self.calls.append(notification.dedup_key)
        if self._error is not None:
            raise self._error


class _FakeDedupStore:
    """NotificationDedupStore 替身：模拟 durable per-channel 状态（delivered 抑制重投）。

    记录每次 should_dispatch/record_failure/record_success 调用，供行为断言。
    非内存 store 由实现方注入——此处只是替身，验证 router 委托给注入的 store。
    """

    def __init__(self) -> None:
        self._status: dict[tuple[str, str, str], str] = {}
        self._claims: dict[tuple[str, str, str], str] = {}
        self.dispatches: list[tuple[str, str, str]] = []
        self.failures: list[tuple[str, str, str, str]] = []
        self.successes: list[tuple[str, str, str]] = []
        self.rejections: list[tuple[str, str, str, str]] = []

    async def should_dispatch(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str
    ) -> str | None:
        key = (str(tenant_id), dedup_key, channel_name)
        self.dispatches.append(key)
        if self._status.get(key) in {"delivered", "rejected"}:
            return None
        self._status.setdefault(key, "pending")
        claim = f"claim-{len(self._claims) + 1}"
        self._claims[key] = claim
        return claim

    async def record_failure(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        claim_token: str,
        error: BaseException,
    ) -> bool:
        key = (str(tenant_id), dedup_key, channel_name)
        if self._claims.get(key) != claim_token:
            return False
        self._status[key] = "pending"
        self.failures.append((*key, type(error).__name__))
        return True

    async def record_success(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str, *, claim_token: str
    ) -> bool:
        key = (str(tenant_id), dedup_key, channel_name)
        if self._claims.get(key) != claim_token:
            return False
        self._status[key] = "delivered"
        self.successes.append(key)
        return True

    async def record_rejection(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        claim_token: str,
        error: PolicyViolation,
    ) -> bool:
        key = (str(tenant_id), dedup_key, channel_name)
        if self._claims.get(key) != claim_token:
            return False
        self._status[key] = "rejected"
        self.rejections.append((*key, type(error).__name__))
        return True


class _FlakyDedupStore:
    """NotificationDedupStore 替身：可配置 ``record_failure``/``record_success`` 写失败。

    模拟 durable store 的 DB 写入抛错（如数据库不可用）；异常消息携带合成秘密，
    用于断言 router 绝不把 store/DB 异常文本泄进 TransientError。
    ``fail_record_success_for`` 按渠道名指定：仅这些渠道的 record_success 抛错，
    其余渠道正常写入——用于观察「store 写失败不中断其他渠道持久化」。
    """

    def __init__(
        self,
        *,
        fail_record_failure: bool = False,
        fail_record_success_for: set[str] | None = None,
    ) -> None:
        self._fail_failure = fail_record_failure
        self._fail_success_for = set(fail_record_success_for or ())
        self._status: dict[tuple[str, str, str], str] = {}
        self._claims: dict[tuple[str, str, str], str] = {}
        self.failures: list[tuple[str, str, str, str]] = []
        self.successes: list[tuple[str, str, str]] = []
        self.rejections: list[tuple[str, str, str, str]] = []

    async def should_dispatch(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str
    ) -> str | None:
        key = (str(tenant_id), dedup_key, channel_name)
        if self._status.get(key) in {"delivered", "rejected"}:
            return None
        self._status.setdefault(key, "pending")
        claim = f"claim-{len(self._claims) + 1}"
        self._claims[key] = claim
        return claim

    async def record_failure(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        claim_token: str,
        error: BaseException,
    ) -> bool:
        if self._fail_failure:
            raise RuntimeError("store 写失败：" + "backend" + "-secret-token")
        key = (str(tenant_id), dedup_key, channel_name)
        if self._claims.get(key) != claim_token:
            return False
        self._status[key] = "pending"
        self.failures.append((*key, type(error).__name__))
        return True

    async def record_success(
        self, tenant_id: TenantId, dedup_key: str, channel_name: str, *, claim_token: str
    ) -> bool:
        if channel_name in self._fail_success_for:
            raise RuntimeError("store 写失败：" + "backend" + "-secret-token")
        key = (str(tenant_id), dedup_key, channel_name)
        if self._claims.get(key) != claim_token:
            return False
        self._status[key] = "delivered"
        self.successes.append(key)
        return True

    async def record_rejection(
        self,
        tenant_id: TenantId,
        dedup_key: str,
        channel_name: str,
        *,
        claim_token: str,
        error: PolicyViolation,
    ) -> bool:
        key = (str(tenant_id), dedup_key, channel_name)
        if self._claims.get(key) != claim_token:
            return False
        self._status[key] = "rejected"
        self.rejections.append((*key, type(error).__name__))
        return True


class _SelectedPolicy:
    """RoutingPolicy 替身：只返回 available 中选中（注册）的渠道。"""

    def __init__(self, selected: list[NotificationChannel]) -> None:
        self._selected = list(selected)

    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        return [ch for ch in available if ch in self._selected]


class _ExplodingPolicy:
    """路由策略替身：模拟携带敏感异常消息的策略故障。"""

    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        raise RuntimeError("policy failed: " + "sk-" + "A1B2C3D4E5F6G7H8I9J0K1L2")


class _BlockingChannel:
    """并发探测渠道：开始投递后等待对端也已开始——顺序实现必然死锁（wait_for 超时）。"""

    def __init__(
        self,
        name: str,
        started: asyncio.Event,
        peer_started: asyncio.Event,
        calls: list[str],
    ) -> None:
        self.name = name
        self._started = started
        self._peer = peer_started
        self._calls = calls

    async def deliver(self, notification: Notification) -> None:
        self._calls.append(self.name)
        self._started.set()
        await self._peer.wait()


# --- NotificationDedupStore / RoutingPolicy 契约（RED：dedup.py / router.py 未建）------


def test_notification_dedup_store_protocol_methods() -> None:
    """NotificationDedupStore Protocol 定义 fencing 与永久拒绝所需方法。"""
    NotificationDedupStore = _load("NotificationDedupStore")
    for name in (
        "should_dispatch",
        "record_failure",
        "record_success",
        "record_rejection",
    ):
        assert hasattr(NotificationDedupStore, name), f"NotificationDedupStore 缺 {name}"


def test_routing_policy_protocol_methods() -> None:
    """RoutingPolicy Protocol 定义 channels_for（policy 拥有渠道选择）。"""
    RoutingPolicy = _load("RoutingPolicy")
    assert hasattr(RoutingPolicy, "channels_for")


def test_router_and_dedup_symbols_exist() -> None:
    """NotificationRouter 从 notification_gateway.router 导入（RED：模块未建）。"""
    NotificationRouter = _load("NotificationRouter")
    assert callable(NotificationRouter)


# --- NotificationRouter：注入 store/policy + 投递行为 ---------------------------------


async def test_router_dispatch_delegates_to_injected_store_and_policy() -> None:
    """router 用注入的 store（非内存）判重、经 policy 选渠道后投递。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FakeDedupStore()
    ch = _FakeChannel("structured_log")
    router = NotificationRouter(store, _SelectedPolicy([ch]))
    router.register_channel(ch)

    n = _notification()
    await router.dispatch(n)

    assert ch.calls == [n.dedup_key]
    assert store.successes == [("t1", n.dedup_key, "structured_log")]
    assert store.dispatches == [("t1", n.dedup_key, "structured_log")]


async def test_urgent_fans_out_concurrently() -> None:
    """URGENT 多渠道并发 fan-out：顺序实现会让 blocking 渠道死锁（wait_for 超时）。"""
    NotificationRouter = _load("NotificationRouter")
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    calls: list[str] = []
    ch_a = _BlockingChannel("a", started_a, started_b, calls)
    ch_b = _BlockingChannel("b", started_b, started_a, calls)
    store = _FakeDedupStore()
    router = NotificationRouter(store, _SelectedPolicy([ch_a, ch_b]))
    router.register_channel(ch_a)
    router.register_channel(ch_b)

    n = _notification(priority=NotificationPriority.URGENT)
    await asyncio.wait_for(router.dispatch(n), timeout=1.0)

    assert set(calls) == {"a", "b"}


async def test_one_channel_failure_does_not_block_others_and_is_retryable() -> None:
    """单渠道失败不影响其他渠道：健康渠道仍投递成功，失败渠道在 store 中记为可重试。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FakeDedupStore()
    failing = _FakeChannel("failing", error=TransientError("backend down"))
    healthy = _FakeChannel("healthy")
    router = NotificationRouter(store, _SelectedPolicy([failing, healthy]))
    router.register_channel(failing)
    router.register_channel(healthy)

    n = _notification()
    with pytest.raises(TransientError) as excinfo:
        await router.dispatch(n)

    # 两个渠道都被尝试；健康渠道投递成功
    assert failing.calls == [n.dedup_key]
    assert healthy.calls == [n.dedup_key]
    assert ("t1", n.dedup_key, "healthy") in store.successes
    # 失败渠道记录为可重试（pending），可被后续 dispatch 续投
    assert ("t1", n.dedup_key, "failing") in [f[:3] for f in store.failures]
    assert excinfo.value.is_retryable


async def test_all_channels_success_no_raise() -> None:
    """全部渠道成功：dispatch 正常返回，不抛错；store 记录全部成功。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FakeDedupStore()
    ch_a = _FakeChannel("a")
    ch_b = _FakeChannel("b")
    router = NotificationRouter(store, _SelectedPolicy([ch_a, ch_b]))
    router.register_channel(ch_a)
    router.register_channel(ch_b)

    n = _notification()
    await router.dispatch(n)

    assert ch_a.calls == [n.dedup_key]
    assert ch_b.calls == [n.dedup_key]
    assert store.successes == [("t1", n.dedup_key, "a"), ("t1", n.dedup_key, "b")]
    assert store.failures == []


async def test_duplicate_dispatch_is_suppressed_by_store() -> None:
    """重复 dispatch（同一 dedup_key、渠道已投递）被 store 抑制，不重复投递。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FakeDedupStore()
    ch = _FakeChannel("structured_log")
    router = NotificationRouter(store, _SelectedPolicy([ch]))
    router.register_channel(ch)

    n = _notification()
    await router.dispatch(n)
    await router.dispatch(n)  # 重复 dispatch

    assert ch.calls == [n.dedup_key]  # 只投递一次
    assert store.successes == [("t1", n.dedup_key, "structured_log")]


async def test_routing_failure_surfaces_transient_error_sanitized() -> None:
    """路由失败以 TransientError 呈现（可重试）；store 只收到脱敏类型名，异常消息不外泄。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FakeDedupStore()
    secret = "backend" + "-secret-token"
    failing = _FakeChannel("failing", error=RuntimeError(secret))
    healthy = _FakeChannel("healthy")
    router = NotificationRouter(store, _SelectedPolicy([failing, healthy]))
    router.register_channel(failing)
    router.register_channel(healthy)

    n = _notification()
    with pytest.raises(TransientError) as excinfo:
        await router.dispatch(n)

    assert excinfo.value.is_retryable
    assert secret not in str(excinfo.value)  # 不阻塞主业务；错误消息脱敏
    assert store.failures == [("t1", n.dedup_key, "failing", "RuntimeError")]


async def test_record_failure_store_write_failure_continues_other_channels() -> None:
    """record_failure 的 store 写失败：继续处理其他渠道的持久化，
    最终只抛固定脱敏、可重试 TransientError（不泄漏 store/DB 异常文本）。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FlakyDedupStore(fail_record_failure=True)
    failing = _FakeChannel("failing", error=TransientError("backend down"))
    healthy = _FakeChannel("healthy")
    router = NotificationRouter(store, _SelectedPolicy([failing, healthy]))
    router.register_channel(failing)
    router.register_channel(healthy)

    n = _notification()
    secret = "backend" + "-secret-token"
    with pytest.raises(TransientError) as excinfo:
        await router.dispatch(n)

    assert excinfo.value.is_retryable
    assert secret not in str(excinfo.value)  # 绝不泄漏 store/DB 异常文本
    assert "RuntimeError" not in str(excinfo.value)
    # 继续处理其他渠道的持久化：健康渠道的 record_success 仍被调用
    assert ("t1", n.dedup_key, "healthy") in store.successes
    # record_failure 抛错，未写入 store
    assert store.failures == []


async def test_record_success_store_write_failure_continues_other_channels() -> None:
    """record_success 的 store 写失败：继续处理其他渠道的持久化，
    最终只抛固定脱敏、可重试 TransientError（不泄漏 store/DB 异常文本）。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FlakyDedupStore(fail_record_success_for={"a"})
    ch_a = _FakeChannel("a")
    ch_b = _FakeChannel("b")
    router = NotificationRouter(store, _SelectedPolicy([ch_a, ch_b]))
    router.register_channel(ch_a)
    router.register_channel(ch_b)

    n = _notification()
    secret = "backend" + "-secret-token"
    with pytest.raises(TransientError) as excinfo:
        await router.dispatch(n)

    assert excinfo.value.is_retryable
    assert secret not in str(excinfo.value)  # 绝不泄漏 store/DB 异常文本
    assert "RuntimeError" not in str(excinfo.value)
    # 继续处理其他渠道的持久化：ch_b 的 record_success 仍被调用
    assert ("t1", n.dedup_key, "b") in store.successes


async def test_policy_owns_channel_selection_not_priority_gate() -> None:
    """router 不内建「LOW→站内」映射（Slice3 无 in-app 渠道）；policy 选中即投递。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FakeDedupStore()
    ch = _FakeChannel("structured_log")
    router = NotificationRouter(store, _SelectedPolicy([ch]))
    router.register_channel(ch)

    n = _notification(priority=NotificationPriority.LOW)
    await router.dispatch(n)

    assert ch.calls == [n.dedup_key]
    assert store.successes == [("t1", n.dedup_key, "structured_log")]


async def test_policy_selection_limits_channels() -> None:
    """policy 未选中的渠道不被投递（router 只投 policy 返回的渠道）。"""
    NotificationRouter = _load("NotificationRouter")
    store = _FakeDedupStore()
    ch_a = _FakeChannel("a")
    ch_b = _FakeChannel("b")
    router = NotificationRouter(store, _SelectedPolicy([ch_a]))
    router.register_channel(ch_a)
    router.register_channel(ch_b)

    n = _notification()
    await router.dispatch(n)

    assert ch_a.calls == [n.dedup_key]
    assert ch_b.calls == []


async def test_policy_exception_is_sanitized_transient_error() -> None:
    """策略异常不得将原始凭证文本传播给调用方。"""
    NotificationRouter = _load("NotificationRouter")
    secret = "sk-" + "A1B2C3D4E5F6G7H8I9J0K1L2"
    router = NotificationRouter(_FakeDedupStore(), _ExplodingPolicy())

    with pytest.raises(TransientError) as excinfo:
        await router.dispatch(_notification())

    assert excinfo.value.is_retryable
    assert secret not in str(excinfo.value)
    assert secret not in excinfo.value.context.values()


async def test_policy_violation_is_terminal_and_does_not_schedule_retry() -> None:
    """渠道策略拒绝是永久结果：持久化 rejected，不记可重试失败。"""
    NotificationRouter = _load("NotificationRouter")
    StructuredLogChannel = _load("StructuredLogChannel")
    store = _FakeDedupStore()
    channel = StructuredLogChannel()
    router = NotificationRouter(store, _SelectedPolicy([channel]))
    router.register_channel(channel)
    external_link = "https://" + "evil.example" + ".com/phish"

    with pytest.raises(PolicyViolation) as excinfo:
        await router.dispatch(_notification(link=external_link))

    assert not excinfo.value.is_retryable
    assert external_link not in str(excinfo.value)
    assert store.failures == []
    assert store.rejections == [
        ("t1", "opp-1:handoff", "structured_log", "PolicyViolation")
    ]
    assert not await store.should_dispatch(TenantId("t1"), "opp-1:handoff", "structured_log")


# --- StructuredLogChannel：固定结构化键 + 深链清理 + 外链/凭证拒绝 --------------------

# 运行时拼接的合成凭证/外链：测试源码不写完整凭证形态（scan_sensitive 纪律）。
def _synthetic_sk_token() -> str:
    # sk- + 24 个字母数字：符合「sk- + 20+ 位」的标准凭证形态，实现方应识别。
    return "sk-" + "A1B2C3D4E5F6G7H8I9J0K1L2"


def _synthetic_password_assignment() -> str:
    return "pass" + "word=" + "sup" + "er" + "secret"


def _synthetic_quoted_password_assignment() -> str:
    """引号包裹的 password 赋值（值本身运行时拼接，敏感扫描同纪律应命中）。"""
    return 'pass' + 'word="' + "sup" + "er" + "secret" + '"'


def _absolute_link() -> str:
    return "https://" + "evil.example" + ".com/phish?x=1"


def _channel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """过滤结构化日志渠道自己的记录（排除测试内其他 logger 噪音）。"""
    return [r for r in caplog.records if r.name == _CHANNEL_LOGGER]


async def test_structured_log_channel_emits_fixed_structured_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogChannel 用固定 logger 与固定结构化键输出通知字段。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    assert channel.name == "structured_log"

    n = _notification(link="/opportunities/opp-1")
    with caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER):
        await channel.deliver(n)

    records = _channel_records(caplog)
    assert len(records) == 1
    d = records[0].__dict__
    assert d.get("tenant_id") == "t1"
    assert d.get("recipient") == "emp-1"
    assert d.get("priority") == NotificationPriority.URGENT.value
    assert d.get("dedup_key") == n.dedup_key
    assert d.get("source_event") == "HandoffRequested"
    assert d.get("title") == n.title
    assert d.get("context") == n.context
    assert d.get("next_step") == "立即接管"
    assert d.get("due_at") is not None
    assert d.get("link") == "/opportunities/opp-1"


async def test_structured_log_channel_strips_query_and_token_from_relative_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """相对深链只输出清理 query/token 后的路径（token/query 数据不落日志）。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(link="/opportunities/opp-1?token=abc&utm_source=x")

    with caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER):
        await channel.deliver(n)

    records = _channel_records(caplog)
    assert len(records) == 1
    assert records[0].__dict__.get("link") == "/opportunities/opp-1"
    assert "token=abc" not in records[0].__dict__.get("link", "")


@pytest.mark.parametrize(
    "link",
    [_absolute_link(), "https://app.example.com/opp/1", "//evil.example.com/x"],
)
async def test_structured_log_channel_rejects_absolute_external_link(
    link: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """绝对外链（含协议相对）被 PolicyViolation 拒绝，且拒绝时不落任何日志。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(link=link)

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation),
    ):
        await channel.deliver(n)

    assert _channel_records(caplog) == []  # 拒绝时绝不写日志


async def test_structured_log_channel_rejects_credential_shaped_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """标题含凭证形态内容（合成 sk- token）被 PolicyViolation 拒绝，不落日志。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(title=f"需要尽快处理 {_synthetic_sk_token()}")

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation),
    ):
        await channel.deliver(n)

    assert _channel_records(caplog) == []


async def test_structured_log_channel_rejects_credential_shaped_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """context 值含凭证形态内容（合成 password 赋值，运行时拼接）被 PolicyViolation 拒绝，不落日志。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(context={"api_key": _synthetic_password_assignment()})

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation),
    ):
        await channel.deliver(n)

    assert _channel_records(caplog) == []


async def test_structured_log_channel_rejects_quoted_password_assignment_in_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """引号包裹的 password 赋值（与敏感扫描同纪律）在标题中被 PolicyViolation 拒绝，不落日志。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(title=f"重置 {_synthetic_quoted_password_assignment()}")

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation),
    ):
        await channel.deliver(n)

    assert _channel_records(caplog) == []


async def test_structured_log_channel_rejects_quoted_password_assignment_in_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """引号包裹的 password 赋值在 context 值中被 PolicyViolation 拒绝，不落日志。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(context={"db": _synthetic_quoted_password_assignment()})

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation),
    ):
        await channel.deliver(n)

    assert _channel_records(caplog) == []


async def test_structured_log_channel_rejects_credential_shaped_next_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """next_step 会落日志的自由文本含凭证形态（合成 sk- token）被 PolicyViolation 拒绝，不落日志。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(next_step=f"请先处理 {_synthetic_sk_token()}")

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation),
    ):
        await channel.deliver(n)

    assert _channel_records(caplog) == []


async def test_structured_log_channel_rejects_credential_shaped_context_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """context key 会落日志的自由文本含凭证形态（合成 sk- token）被 PolicyViolation 拒绝，不落日志。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    n = _notification(context={_synthetic_sk_token(): "value"})

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation),
    ):
        await channel.deliver(n)

    assert _channel_records(caplog) == []


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("source_event", {"source_event": _synthetic_sk_token()}),
        ("dedup_key", {"dedup_key": _synthetic_sk_token()}),
    ],
)
async def test_structured_log_channel_rejects_credential_shaped_logged_identifier(
    field: str,
    kwargs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """source_event/dedup_key 也会落日志，含凭证时必须在写入前拒绝。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation) as excinfo,
    ):
        await channel.deliver(_notification(**kwargs))

    assert _synthetic_sk_token() not in str(excinfo.value), field
    assert _channel_records(caplog) == []


async def test_structured_log_channel_context_key_rejection_does_not_echo_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """拒绝 context key 时，异常文本本身不能回显被拒绝的凭证。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    secret = _synthetic_sk_token()

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation) as excinfo,
    ):
        await channel.deliver(_notification(context={secret: "value"}))

    assert secret not in str(excinfo.value)
    assert _channel_records(caplog) == []


@pytest.mark.parametrize("link", [r"/\evil.example/path", "/%5Cevil.example/path", "/ok\x1fpath"])
async def test_structured_log_channel_rejects_noncanonical_relative_link(
    link: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """反斜杠、编码反斜杠和控制字符不能绕过相对深链政策。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation) as excinfo,
    ):
        await channel.deliver(_notification(link=link))

    assert link not in str(excinfo.value)
    assert _channel_records(caplog) == []


async def test_structured_log_channel_rejects_malformed_protocol_relative_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """畸形 protocol-relative URL 必须固定拒绝，不能让 URL parser 的 ValueError 外泄。"""
    StructuredLogChannel = _load("StructuredLogChannel")
    channel = StructuredLogChannel()
    malformed_link = "/" + "/" + "["

    with (
        caplog.at_level(logging.INFO, logger=_CHANNEL_LOGGER),
        pytest.raises(PolicyViolation) as excinfo,
    ):
        await channel.deliver(_notification(link=malformed_link))

    assert malformed_link not in str(excinfo.value)
    assert _channel_records(caplog) == []
