# ADR 0007：EventHandler 泛型逆变

- **状态**：已接受
- **日期**：2026-08-08

## 背景

骨架阶段对 `shared/events/bus.py` 跑 `mypy domains shared tool_gateway` 时报一处真实类型错误：

```text
shared/events/bus.py:28: error: Invariant type variable "E" used in protocol where contravariant one is expected  [misc]
```

`E = TypeVar("E", bound=DomainEvent)` 声明为不变（invariant），但 `E` 在 `EventHandler` 协议里只出现在消费（参数）位置：

```python
class EventHandler(Protocol[E]):
    async def handle(self, event: E) -> None: ...        # E 只在参数位

class EventBus(Protocol):
    def subscribe(self, event_type: type[E], handler: EventHandler[E]) -> None: ...  # 仍只消费 E
```

按协议子类型规则，这样的 TypeVar 应为**逆变**（contravariant）。mypy 在 invariant 下正确报了错——它拦的正是声明与使用的不匹配。

约束：错误必须消除，但**不能用配置涂绿**（模块级 `disable_error_code` 会掩盖后续真实类型错误），且骨架实现尚未开始，改动必须最小。

## 决策

把 `shared/events/bus.py` 第 24 行改为：

```python
E = TypeVar("E", bound=DomainEvent, contravariant=True)
```

只改这一行，其余代码与文档不改。mypy 实测从「1 error」变为「Success: no issues found in 137 source files」，`python3 scripts/check_boundaries.py --skeleton` 保持全绿。

## 理由

- **方向正确**：`E` 在协议里全部出现在消费位，逆变 TypeVar 是静态检查层面的正确表达。它让「接受基类事件的处理器可用于派生事件」——`EventHandler[DomainEvent]` 可代入需要 `EventHandler[派生事件]` 的位置。这正是订阅语义：能处理基类事件的处理器，必然能处理它的派生事件。
- **零运行时影响**：`@runtime_checkable` 只检查方法存在性；TypeVar 方差是纯静态类型层面的属性，不改变 `handle` / `subscribe` 的运行行为、事件字段、Provenance 或 Money 语义。
- **无外溢**：全库仅 `bus.py` 自身使用 `EventHandler` 与 `subscribe`，无任何域文件引用，改动不会波及其他模块的类型检查。
- **最小**：一行改动解决真实错误，为后续切片（域 events.py 的订阅装配）扫清类型障碍。

## 放弃的选项

**对 `shared.events.bus` 做模块级 `disable_error_code = ["misc"]`。** 好在哪里：不用碰 shared 就能让 CI 变绿。为什么现在不选：配置涂绿会掩盖该模块此后出现的真实类型错误，等于关闭这个文件里所有 `misc` 类检查；Phase 0 计划也明确不新增任何 mypy `misc` 豁免。什么条件下会重新考虑：不会——正确做法是修类型，不是关检查。

**保留不变 TypeVar 并绕开。** 好在哪里：零改动。为什么现在不选：mypy 会持续报错，CI 无法全绿；若在订阅处加 `type: ignore` 绕开，只是把错误挪到消费方，风险反而扩散到更多文件。什么条件下会重新考虑：无——错误本身说明声明与使用不匹配，应修声明。

**拆成两个 TypeVar（一个逆变一个协变）以备将来协变使用。** 好在哪里：给未来「处理器返回事件」留了空间。为什么现在不选：当前 `E` 没有任何协变使用位，双 TypeVar 是过度设计，会让协议签名复杂化。什么条件下会重新考虑：若将来 `EventHandler` 出现返回 `E` 的位置（如处理器产出事件），届时再评估。

## 后果

- 静态层面 `EventHandler[DomainEvent]` 成为 `EventHandler[具体派生事件]` 的子类型，符合订阅语义。
- 这是对共享公共契约的签名修正，属公共 API 变更，故本 ADR 留档。
- 无运行时行为、事件字段、Provenance、Money 语义变化；`shared/events/catalog.py` 的事件定义不受影响。
- 骨架期 stub 纯度不受影响（改动不在任何函数体）。

## 何时重新审视

1. 若将来 `EventHandler` 协议出现需要协变使用 `E` 的位置（例如处理器返回事件、或引入泛型化的处理结果类型），重新评估方差设计。
2. 若引入第二个事件处理抽象，或对协议做 `isinstance` / 运行时动态分发的检查，需复核方差与运行时可检查性是否仍自洽。

触发条件是具体的新使用形态，不是「以后再看」。
