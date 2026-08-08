# Slice 1 · shared 契约实现计划

> **给执行者的说明**：本计划只覆盖 HANDBOOK 切片 1（`shared/` 全部契约实现 + 单测），不实现任何业务域，不改 `catalog.py` 事件字段，不实现 `EventBus` outbox（HANDBOOK 明确延后到切片 2）。基于对 `shared/` 全部文件的审计编写，事实与提议分开。
>
> 纪律沿用 Phase 0：每个任务小到可独立 review、TDD（先 RED 后 GREEN）、每任务提交前 `python3 scripts/check_boundaries.py` 全绿、每任务单独 commit + 单独 push、每任务后 `make check` 保持全绿。

**目标**：让 `shared/` 的契约真正可用且可测——`Money`/`FxRate`/`convert`、`derive_confidence`、`Provenance` 三件套、`new_id`、`TradeOSError` 结构化上下文、`EventEnvelope`。为后续域切片提供经过单测的公共契约。

**执行环境**：`tradeos-py312`（conda，Python 3.12.13）。所有 Python 命令在该环境执行；`python3 scripts/check_boundaries.py` 用系统 python3。

---

## 审计：事实与提议

### 一、事实类（文档已定、stub 未实现或引用缺失）

| # | 位置 | 事实 |
|---|---|---|
| F1 | `shared/schemas/money.py` | `add`/`multiply` docstring 引用 `CurrencyMismatchError`，但**该错误类型全库未定义**。 |
| F2 | `Money.__post_init__` | 文档要求 `amount` 必须为 `Decimal`，`float` 直接抛错、不得静默转换。 |
| F3 | `Money.multiply` | 文档要求「只允许乘 `Decimal` 或 `int`」；当前签名 `factor: Decimal` 未反映 `int`。 |
| F4 | `Money.add` | 同币种相加；币种不一致抛 `CurrencyMismatchError`，不自动换算。 |
| F5 | `Money.round_to` | 显式精度与舍入策略，统一默认 `ROUND_HALF_UP`。 |
| F6 | `FxRate` / `convert` | `rate.base == amount.currency` 且 `rate.quote == to`，不匹配抛错；不自动取倒数；`convert` 不做舍入（调用方决定精度）。 |
| F7 | `shared/schemas/evidence.py` | `derive_confidence` 五条规则按序应用（最高档为基准 → 同级独立证据上浮一档 → 矛盾下浮一档 → 全量过期下浮一档 → 钳制到 `[LOW, EXTREME]`）；空证据列表抛错；纯函数、`now` 由调用方传入。 |
| F8 | `meets_threshold` | 按档位有序比较（`tier >= minimum`）。档位顺序由枚举定义。 |
| F9 | `Provenance.__post_init__` | `WEB_PAGE` → `source_url` 与 `page_hash` 必填；`confirmed_by` 与 `confirmed_at` 同有同无。 |
| F10 | `FactualField` | 拒绝 `AGENT_INFERENCE` 来源（属于 `InferredField`）。 |
| F11 | `InferredField` | `based_on` 不得为空。 |
| F12 | `new_id` | 时间有序（ULID 或 UUIDv7）、`<前缀>_<唯一部分>` 格式。**Python 3.12 无 `uuid.uuid7`**，且应避免引入第三方依赖 → 提议用纯 stdlib ULID。 |
| F13 | `TradeOSError` | docstring 要求「带结构化上下文（`tenant_id`、相关实体 ID）」；当前类只有 `is_retryable`，无上下文机制。 |
| F14 | `EventEnvelope` | docstring 已定义字段 `event_id` / `attempt` / `published_at` / `trace_id`；当前是空 stub 类。 |
| F15 | `EventBus` outbox | HANDBOOK 明确「依赖切片 2 的库，可后补」→ **延后到切片 2**。 |

### 二、提议类决策（文档未明确，本计划给出解释性选择，供监督确认）

| # | 决策 | 依据 / 取舍 |
|---|---|---|
| P1 | `CurrencyMismatchError` 放 `shared/errors.py`，作为 `ValidationError` 子类（不可重试）。 | 币种不匹配是「输入错」→ 归 `ValidationError`；放 errors.py 使全库可统一引用。 |
| P2 | `multiply` 签名改为 `factor: Decimal \| int`；运行时显式拒绝 `float` 与 `bool`（`bool` 是 `int` 子类，必须单独拦）。 | 对齐 F3 文档；`bool` 乘数会让数量语义错乱。 |
| P3 | `Money`/`FxRate` 校验币种为 **3 位大写 ASCII**（对 `CurrencyCode` 文档「ISO 4217 三字母」的最小实现）。 | 不做 ISO 全表校验（维护成本高、收益低）；大写三字母是合理的最小约束。 |
| P4 | `FxRate.__post_init__`：`rate` 必须为正 `Decimal`；`base != quote`。 | 文档未明说，但零/负汇率、同币种换算都是错误输入。 |
| P5 | `Money` 输入错误抛 `ValidationError`（float/int/bool amount、非法币种、负 `places`、非法舍入策略）；`round_to` 校验 `places >= 0` 且 `strategy` 是合法 `ROUND_*` 常量。 | 与 shared.errors 分类一致；`ROUND_HALF_UP` 为统一默认值（F5）。 |
| P6 | 证据「独立」的操作定义：**`source_id` 不同即独立**（`source_id` 即页面哈希/消息 ID，「同一页面抓两次」→ 同 `source_id` → 不独立）；同级独立证据 ≥2 条只上浮**一档**（文档「可上浮一档」，不随条数累加）。 | 文档「且来源不是同一页面/同一条消息」是解释 `source_id` 为何能区分，操作上由 `source_id` 承载。 |
| P7 | `conflicting_pairs` 中未出现在证据列表的 `source_id`：宽松忽略（不抛错）。 | 矛盾对是调用方业务语义的提示；多余条目不应让推导崩溃。 |
| P8 | `applied_rules` 用稳定规则名：`base_from_highest` / `independence_boost` / `conflict_penalty` / `staleness_penalty` / `clamp`；`explanation` 断言非空且提及基准档等级与命中的规则。 | 便于单测回溯；解释要能回答「为什么是这一档」。 |
| P9 | `Provenance` 补非空校验：`extracted_by`、`source_id` 非空（`strip()` 后）。 | 文档要求 `extracted_by` 记具体模型版本；空值会让留痕失效。 |
| P10 | `ExtractionChain`：`employee_edited` / `edited_by` / `edited_at` **三者同有同无**。 | 对齐「`None` 表示未修改」；部分赋值是数据完整性错误。 |
| P11 | `new_id` 用纯 stdlib ULID：48-bit 毫秒时间戳 + 80-bit 随机 + Crockford base32（26 字符）；暴露可测纯函数 `_encode_ulid(ts_ms, rand_bytes)`；`new_id(prefix)` 用 `time.time_ns()` + `secrets`；`prefix` 非空校验。 | 满足 F12 且零依赖；纯函数可测时间有序性。 |
| P12 | `TradeOSError.__init__(message, *, context: Mapping[str, str] | None = None)` 存 `self.context: dict[str, str]`。 | 实现 F13 文档；additive、向后兼容（子类无需改动，`raise X("msg")` 照常）。 |
| P13 | `EventEnvelope` 用 `@dataclass(frozen=True)`；`attempt` 校验 `>= 1`。 | 实现 F14；投递序号从 1 起。 |
| P14 | `provenance.py` 的 `Generic[T]` 类（`FactualField`/`InferredField`/`ExtractionChain`）迁移为 **PEP 695** `class X[T]`，从而消除 `UP046`，并**在同一 commit 删除 pyproject 里该文件的 `UP046` 豁免**。 | 纯语法迁移、语义等价、公共表面不变；满足任务 1 的豁免退出条件。需以 mypy/pytest/`ruff --no-cache` 验证（3.12 下 dataclass + PEP 695 泛型按实测为准）。 |

### 三、ADR 决策（显式）

依据 `shared/AGENTS.md §改动约束`（枚举触发：改事件字段 / 改 Provenance 结构 / 加新事件）与「实现已文档化行为 ≠ 重新设计字段」的原则：

- **实现 F1–F15 中已文档化的 stub 行为 → 无需 ADR**（履约，非契约变更）。
- **新增 `CurrencyMismatchError`、`TradeOSError.context` → 无需 ADR**（additive、向后兼容，类比「加新事件不影响现有订阅方」）。
- **`multiply` 标注 `Decimal | int` → 无需 ADR**（标注对齐 F3 文档，非语义变更）。
- **PEP 695 语法迁移（P14）→ 无需 ADR**（纯语法、语义等价、公共表面不变）；与任务 0 方差修复（改变类型语义、需 ADR）相区分。若监督希望按「任何 shared 改动都留 ADR」的极严格解释执行，可补一张轻量 ADR，成本极低——计划已列明取舍。
- **P3/P4/P5/P6/P7/P9/P10/P13 的新增校验 → 无需 ADR**（对既有字段施加约束，不改字段本身；作为解释性决策列于本节供监督确认）。
- **`catalog.py` 事件字段不改**（改事件字段必须留 ADR，且骨架 `= None  # type: ignore[assignment]` / `= ""` / `= 0` 占位属于重新设计 → 延后到对应域实现时按 ADR 处理）。

### 四、范围边界

- `catalog.py` 本切片**不改**（F15 相邻：事件字段是骨架占位，改 = ADR + 重新设计）。
- `bus.py` 只做 **`EventEnvelope` 纯契约**；`EventBus` outbox 实现延后到切片 2。
- `--skeleton`：切片 1 开始实现 shared，stub-purity 会在已实现文件上报（预期过渡）；`make check` 用常规 `check_boundaries.py`（已不含 `--skeleton`），`check:skeleton` 目标保留供手工使用、不再作为验收门槛。

---

## 全局约束

- **九条硬边界**（根 AGENTS.md §三）：本切片直接落地 2/3/4/5/8——金额只用 `Decimal`（禁 float）、置信度不存数值、字段带 Provenance、事实与推断分离、强类型 `TenantId`。
- **共享公共 API 保守**：只实现已文档化行为；改动字段/结构必须留 ADR（见第三节）。
- **Decimal**：`Money`/`FxRate` 的 `amount`/`rate` 一律 `Decimal`，运行时拒绝 `float`/`bool`/`int`（金额位）。
- **无 Any/type-ignore 压制**：类型问题就地修复，不新增 `# type: ignore`。
- **每任务**：提交前 `python3 scripts/check_boundaries.py` 全绿；`make check` 全绿；单独 commit + 单独 push。
- **Ruff 豁免退出条件（本切片内）**：`shared/events/bus.py` 的 `["PYI013","PIE790"]` 在 S1-6 同一 commit 删除；`shared/schemas/provenance.py` 的 `["UP046"]` 在 S1-4 同一 commit 删除。其余豁免与切片 1 无关，保留到对应文件实现时再删。
- **新符号导入**：测试对「本任务才新增的符号」（如 `CurrencyMismatchError`）用函数内延迟导入，保证 RED 是行为失败而非收集错误。

---

## 任务列表

### 任务 S1-1：errors.py — TradeOSError 结构化上下文 + CurrencyMismatchError

**文件**
- Create: `tests/unit/test_errors.py`
- Modify: `shared/errors.py`

**测试（行为 / 拦截的变异）**
1. `TradeOSError("m", context={"tenant_id": "t1"}).context == {"tenant_id": "t1"}`；不传 context → `{}`。（变异：若 `__init__` 没存 context）
2. 子类 `ValidationError("m", context={...})` 继承同一机制。（变异：若只改基类不兼容子类）
3. `TradeOSError.is_retryable is False`、`TransientError.is_retryable is True`。（变异：若重试标志被改）
4. `issubclass(CurrencyMismatchError, ValidationError)` 且 `CurrencyMismatchError("m").is_retryable is False`。（变异：若错误挂错层级）
5. 错误消息可读、不因 context 而包含密钥类内容（断言 context 与消息分离存）。 （变异：若把 context 拼进消息）

**RED / GREEN**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_errors.py -q   # RED：TradeOSError 无 .context；CurrencyMismatchError 未定义
# 实现 shared/errors.py：TradeOSError.__init__(message, *, context=None)；新增 CurrencyMismatchError(ValidationError)
conda run -n tradeos-py312 pytest tests/unit/test_errors.py -q   # GREEN
```

**验收**
```bash
conda run -n tradeos-py312 make check
python3 scripts/check_boundaries.py
```

**commit**：`feat(shared): add TradeOSError structured context and CurrencyMismatchError` — **push**

---

### 任务 S1-2：money.py — Money / FxRate / convert / PriceBasis

**文件**
- Create: `tests/unit/test_money.py`
- Modify: `shared/schemas/money.py`

**测试（行为 / 拦截的变异）**
1. `Money(Decimal("5000"), "USD")` 合法；`amount=1.0`（float）、`amount=5000`（int）、`amount=True`（bool）均抛 `ValidationError`。（变异：若 `__post_init__` 漏拒 float/int/bool）
2. `add` 同币种求和正确；异币种抛 `CurrencyMismatchError`。（变异：若异币种静默换算或抛错类型错）
3. `multiply(Decimal("3"))`、`multiply(3)` 合法且值正确；`multiply(1.5)`（float）、`multiply(True)`（bool）、`multiply(Money(...))` 抛错。（变异：若放行 float/bool 或两个 Money 相乘）
4. `round_to(2)` 用默认 `ROUND_HALF_UP`（如 `Decimal("1.005") → 1.01`）；`round_to(0, "ROUND_DOWN")` 生效；`places < 0`、非法 strategy 抛错。（变异：若依赖默认舍入或不校验策略）
5. 币种非法（非 3 位大写）抛 `ValidationError`。（变异：若币种格式不校验）
6. `FxRate`：合法构造；`rate=1.0`/`rate<=0`/`base==quote`/币种非法均抛错。（变异：若汇率校验缺失）
7. `convert`：`rate.base == amount.currency` 且 `rate.quote == to` 时返回 `to` 币种、不做舍入；方向不匹配抛错（正向不取倒数）。（变异：若方向校验缺失或自动取倒数）
8. `PriceBasis.INDICATIVE == "indicative"`、`QUOTED == "quoted"`。（变异：若硬边界 7 常量被改）

**RED / GREEN**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_money.py -q    # RED：各方法 NotImplementedError
# 实现 money.py（含 P1–P5 决策）
conda run -n tradeos-py312 pytest tests/unit/test_money.py -q    # GREEN
```

**验收**
```bash
conda run -n tradeos-py312 make check
python3 scripts/check_boundaries.py
```

**commit**：`feat(shared): implement Money FxRate convert contracts` — **push**

---

### 任务 S1-3：evidence.py — derive_confidence 五条规则 + meets_threshold

**文件**
- Create: `tests/unit/test_evidence.py`
- Modify: `shared/schemas/evidence.py`

**测试（行为 / 拦截的变异）**
1. 等级→基准档映射：7 个 `EvidenceLevel` 分别推导到 `LOW`/`LOW_MID`/`MID`/`MID_HIGH`/`HIGH`/`VERY_HIGH`/`EXTREME`。（变异：若映射错位）
2. 同级独立证据上浮一档：2 条不同 `source_id` → +1；同 `source_id` 两次 → 不浮动；3 条独立 → 仍只 +1。（变异：若独立性判断错或累加上浮）
3. 矛盾证据下浮一档并 `has_conflict=True`；`conflicting_pairs` 含未知 `source_id` 时忽略。（变异：若矛盾处理错）
4. 全部证据过期 → 下浮一档并 `is_stale=True`；至少一条新鲜 → 不置 stale。（变异：若过期判定错）
5. 钳制：`EXTREME` 基准 + 独立上浮仍为 `EXTREME`；`LOW` 基准 + 矛盾/过期下浮仍为 `LOW`。（变异：若越界）
6. 空证据列表抛错（不是返回 LOW）。（变异：若把「无证据」当 LOW）
7. `explanation` 非空且提及基准等级描述；`applied_rules` 含命中规则名（P8）。（变异：若解释空洞/规则名不稳定）
8. `meets_threshold(result, minimum)`：`HIGH >= MID_HIGH` 为真、`LOW < MID` 为假。（变异：若档位比较错）

**RED / GREEN**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_evidence.py -q  # RED：derive_confidence/meets_threshold NotImplementedError
# 实现 evidence.py（含 P6–P8 决策）
conda run -n tradeos-py312 pytest tests/unit/test_evidence.py -q  # GREEN
```

**验收**
```bash
conda run -n tradeos-py312 make check
python3 scripts/check_boundaries.py
```

**commit**：`feat(shared): implement derive_confidence tier rules` — **push**

---

### 任务 S1-4：provenance.py — 不变式 + PEP 695 + 删除 UP046 豁免

**文件**
- Create: `tests/unit/test_provenance.py`
- Modify: `shared/schemas/provenance.py`
- Modify: `pyproject.toml`（删除 `"shared/schemas/provenance.py" = ["UP046"]`）

**测试（行为 / 拦截的变异）**
1. `Provenance`：`WEB_PAGE` 缺 `source_url` 或 `page_hash` 抛错；非网页可缺省。（变异：若网页留痕校验缺失）
2. `confirmed_by` 有、`confirmed_at` 无（或反之）抛错；两者同有或同无合法。（变异：若确认时间不配对）
3. `extracted_by`/`source_id` 为空白抛错（P9）。（变异：若留痕字段可空）
4. `is_human_confirmed`：有 `confirmed_by` 为 True、否则 False。（变异：若判定逻辑错）
5. `FactualField`：`provenance.source_type == AGENT_INFERENCE` 抛错；其它类型合法。（变异：若事实字段放行推断）
6. `InferredField`：空 `based_on` 抛错；非空合法。（变异：若无依据推断放行）
7. `ExtractionChain`：`employee_edited`/`edited_by`/`edited_at` 三者同有同无（P10）。（变异：若部分赋值放行）
8. `FactualField[int](...)` 等 PEP 695 泛型可实例化；`ruff check . --no-cache` 0 错误（证明 UP046 不再触发）。（变异：若 PEP 695 迁移失败或豁免残留）

**RED / GREEN**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_provenance.py -q  # RED：各 __post_init__ NotImplementedError
# 实现 provenance.py（不变式 + PEP 695）；同 commit 删除 pyproject 的 UP046 豁免
conda run -n tradeos-py312 pytest tests/unit/test_provenance.py -q  # GREEN
conda run -n tradeos-py312 ruff check . --no-cache                 # 0 错误
```

**验收**
```bash
conda run -n tradeos-py312 make check
python3 scripts/check_boundaries.py
```

**commit**：`feat(shared): implement provenance invariants with PEP 695 generics` — **push**

---

### 任务 S1-5：identifiers.py — stdlib ULID new_id

**文件**
- Create: `tests/unit/test_identifiers.py`
- Modify: `shared/schemas/identifiers.py`

**测试（行为 / 拦截的变异）**
1. `_encode_ulid(ts_ms, rand_bytes)` 输出 26 字符 Crockford base32（字符集 `0123456789ABCDEFGHJKMNPQRSTVWXYZ`）。（变异：若编码字符集/长度错）
2. `_encode_ulid(t1, r) < _encode_ulid(t2, r)` 当 `t1 < t2`（时间有序）。（变异：若时间戳不占高位）
3. `_encode_ulid(t, r1) != _encode_ulid(t, r2)` 当 `r1 != r2`（同毫秒随机）。（变异：若随机位丢失）
4. 解码首 10 字符还原时间戳（供日志/排序核验）。（变异：若编码错位）
5. `new_id("opp")` 形如 `opp_` + 26 字符 base32；两次调用不同；空 prefix 抛错。（变异：若格式/前缀校验缺失）

**RED / GREEN**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_identifiers.py -q  # RED：new_id NotImplementedError、_encode_ulid 未定义
# 实现 identifiers.py（纯 stdlib：time + secrets + base32）
conda run -n tradeos-py312 pytest tests/unit/test_identifiers.py -q  # GREEN
```

**验收**
```bash
conda run -n tradeos-py312 make check
python3 scripts/check_boundaries.py
```

**commit**：`feat(shared): implement stdlib ULID new_id` — **push**

---

### 任务 S1-6：bus.py — EventEnvelope 纯契约 + 删除 PYI013/PIE790 豁免

**文件**
- Create: `tests/unit/test_events.py`
- Modify: `shared/events/bus.py`
- Modify: `pyproject.toml`（删除 `"shared/events/bus.py" = ["PYI013", "PIE790"]`）

**测试（行为 / 拦截的变异）**
1. `EventEnvelope(event_id=..., attempt=1, published_at=..., trace_id=...)` 可构造，字段正确。（变异：若字段未实现）
2. frozen：构造后赋值抛 `FrozenInstanceError`。（变异：若可修改）
3. `attempt < 1` 抛错（P13）。（变异：若投递序号不校验）
4. 事件处理器运行时检查：实现 `async def handle(self, event)` 的类被 `isinstance(x, EventHandler)` 识别（`@runtime_checkable`）。（变异：若协议装饰器丢失）
5. `ruff check . --no-cache` 0 错误（证明 PYI013/PIE790 不再触发）。（变异：若豁免残留）

**RED / GREEN**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_events.py -q     # RED：EventEnvelope 是空 stub
# 实现 bus.py：EventEnvelope 为 frozen dataclass（F14/P13）；同 commit 删除 pyproject 豁免
conda run -n tradeos-py312 pytest tests/unit/test_events.py -q     # GREEN
conda run -n tradeos-py312 ruff check . --no-cache                # 0 错误
```

**验收**
```bash
conda run -n tradeos-py312 make check
python3 scripts/check_boundaries.py
```

**commit**：`feat(shared): define EventEnvelope delivery envelope` — **push**

---

### 任务 S1-7：Slice 1 演示脚本

**文件**
- Create: `scripts/demo_shared_contracts.py`

**实现**
- 一个可运行的演示：`Money` 加法/乘法/舍入、异币种抛 `CurrencyMismatchError`、`derive_confidence` 输出离散档位与解释、`FactualField`/`InferredField`/`Provenance`、`new_id("opp")` 的 ULID 格式、`TradeOSError` 结构化 context。打印可读的走查输出，体现「确定性代码算数字、模型只分类、置信度离散、留痕可追溯」。

**验收**
```bash
conda run -n tradeos-py312 python scripts/demo_shared_contracts.py   # 干净退出、输出走查
conda run -n tradeos-py312 ruff check scripts/demo_shared_contracts.py
python3 scripts/check_boundaries.py
```

**commit**：`docs(shared): add slice 1 shared contract demo` — **push**

---

## Slice 1 验收汇总

```bash
conda run -n tradeos-py312 make check          # ruff / mypy / check_boundaries / pytest 全绿
conda run -n tradeos-py312 python scripts/demo_shared_contracts.py   # 演示可跑
git diff --check
```

- 七个任务 → 七个独立 commit + 七次独立 push。
- `shared/schemas/provenance.py`（UP046）与 `shared/events/bus.py`（PYI013/PIE790）的 Ruff 豁免已按退出条件删除。
- `catalog.py` 未改动；`EventBus` outbox 未实现（延后切片 2）。
- 实现开始后 `check:skeleton` 会在已实现 shared 文件上报 stub-purity（预期过渡），不再作为验收门槛。

## 风险与说明

- **PEP 695 + dataclass（P14）**：Python 3.12 下 `@dataclass class FactualField[T]:` 需以 mypy/pytest 实测确认；若遇不可行，退回 `Generic[T]` 并保留 UP046 豁免，且向监督说明（不静默）。
- **`explanation` 具体性（P8）**：断言粒度是「非空 + 提及基准等级/命中规则」，不锁死文案——避免把解释文本变成脆弱快照。
- **独立证据定义（P6）**：以 `source_id` 判独立是对文档「来源不是同一页面/同一条消息」的操作化解释，属解释性决策，若监督有不同语义请指出。
- **新增校验（P3/P4/P5/P9/P10/P13）**：均为对既有字段的约束，不改字段结构；若监督认为某项过严/过松，调整测试即可。
