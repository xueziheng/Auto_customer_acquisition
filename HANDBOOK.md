# TradeOS 实现手册

骨架已经就位：目录、边界、契约、状态机、接口签名都有了，但函数体全是 `NotImplementedError`。这份手册回答一个问题：**怎么把它变成能跑的系统，且过程中架构不腐化。**

读者是接手实现的人（或 AI）。架构**为什么**这样设计在 `docs/architecture/` 和 `docs/adr/`，这里只讲**怎么做**。

---

## 一、上手：先读这五样

按顺序，约 40 分钟。跳过任何一项都会在后面付出代价。

| 顺序 | 读什么 | 你要拿到什么 |
|---|---|---|
| 1 | [AGENTS.md](AGENTS.md) | 九条硬边界。**这是唯一必须背下来的东西** |
| 2 | [GLOSSARY.md](GLOSSARY.md) | 术语。写代码时用词必须和它一致 |
| 3 | [docs/architecture/01-domain-model.md](docs/architecture/01-domain-model.md) | 四层需求、证据等级、状态机 |
| 4 | [docs/architecture/02-boundaries.md](docs/architecture/02-boundaries.md) | 依赖方向、四个插件点 |
| 5 | `domains/demand/` 全部七个文件 | 域的标准长相。后面每个域都照它写 |

然后跑一次自检，确认环境正常：

```bash
python3 scripts/check_boundaries.py --skeleton
```

### 三个最容易理解错的地方

**「已验证需求」不是状态字段，是门槛。** `NeedHypothesis` 升级为 `ValidatedNeed` 只有一条路：客户本人说过。Agent 推断一百条也不行。整个系统的商业价值取决于这个数字可信——一旦开后门，它就变成噪音，而且信任丢了就回不来了。

**置信度不是模型输出的。** 模型只判断「这条证据属于哪一档」，档位由 `shared/schemas/evidence.py` 的 `derive_confidence` 用规则推出。数据库里没有 `confidence` 数值列，这是有意的。

**Campaign 是授权书不是配置。** 老板批准一个边界（哪些市场、哪些品类、几封信、每天多少），Agent 在边界内自主工作不用逐次请示，越界的动作被 Tool Gateway 拒绝。改边界 = 新版本 + 重新审批。

---

## 二、第 0 步：补齐工程基建

骨架**故意没做**这部分——它和架构无关，且技术选型会随实现细节调整。但没有它一行业务代码都跑不起来，所以这是第一件事。

### 要建的东西

```text
pyproject.toml            依赖与工具配置
alembic.ini + migrations/ 数据库迁移
infra/db/base.py          Repository 基类（租户过滤统一注入）
tests/conftest.py         测试夹具（内存/容器数据库）
Makefile                  常用命令入口
.github/workflows/ci.yml  CI
```

### 依赖清单

```toml
# 运行时
fastapi, uvicorn, pydantic>=2, sqlalchemy>=2, asyncpg, alembic,
redis, boto3, python-dotenv, openai

# 开发
pytest, pytest-asyncio, testcontainers, ruff, mypy
```

### 最关键的一件：Repository 基类

**租户过滤必须在基类统一注入，不能靠每个查询点自觉**（硬边界 8）。这是唯一能真正守住多租户隔离的做法——靠 review 盯每条 SQL 迟早会漏一条，而漏一条就是跨租户数据泄露，且测试环境发现不了。

```python
# infra/db/base.py 的设计要点（实现时展开）
class TenantScopedRepository:
    """所有 Repository 的基类。

    - 构造时绑定 tenant_id
    - 提供的 query 入口自动 WHERE tenant_id = :tenant
    - 提供 unsafe_cross_tenant_query() 供平台运维专用路径，
      调用它必须写审计——把后门做成显式且留痕的，比没有后门更现实
    """
```

### CI 必须跑的四件

```bash
ruff check .                              # 风格
mypy domains shared tool_gateway connectors  # 类型
python3 scripts/check_boundaries.py       # 结构边界 ← 别省
pytest                                    # 测试
```

`check_boundaries.py` 已经覆盖了依赖方向检查，不必再引入 import-linter；两套工具管同一件事只会互相漂移。

### 验收

`make dev` 能起数据库、`make test` 跑通空测试、`make check` 四件全绿。

---

## 三、实现顺序：从链路末端倒着建

### 为什么倒着建

直觉是从「需求发现」开始，因为它是链路第一环。**但那样会很久看不到能用的东西**，而且发现环节的产出无处可去，验证不了对错。

链路末端（机会、接管）不依赖前端。倒着建的好处：

- 每建一段，前面用手工录入喂数据就能立刻用起来
- 接上自动化时，下游已经被真实数据验证过了
- 任何时刻停下来，系统都是可用的，不是半成品

```text
建的顺序   ←──────────────────────────────────────
业务顺序   发现 → 触达 → 回复 → 已验证需求 → 机会 → 接管
           ⑥      ⑤      ④              ②③      ②③
```

### 七个切片

每个切片结束时系统都应该**能演示**。演示不了说明切片切错了。

---

#### 切片 1 · shared 契约层

**做什么**：`shared/` 全部实现 + 单测。纯函数，无 IO，一两天的活。

```text
schemas/money.py        Money 的 Decimal 校验、币种不匹配抛错、convert
schemas/evidence.py     derive_confidence 的五条推导规则
schemas/provenance.py   FactualField / InferredField 的构造校验
schemas/identifiers.py  new_id（ULID 或 UUIDv7）
events/bus.py           EventBus 的 outbox 实现（依赖切片 2 的库，可后补）
```

**为什么第一个**：所有域都依赖它。它错了，上面全错，且错得很隐蔽（金额精度问题不会立刻暴露）。

**验收**：`derive_confidence` 的每条规则至少一组用例；`Money` 传 float 抛错的用例；`FactualField` 拒绝 `AGENT_INFERENCE` 来源的用例。

**别做**：别在这里加任何业务判断。`shared` 定义「金额长什么样」，不定义「什么价格算低」。

---

#### 切片 2 · 持久化基建 + 机会域

**做什么**：把 `domains/opportunities` 完整落地，含数据库表、Repository 实现、打分。

```text
migrations/            opportunities / score_snapshots / handoffs / loss_records
infra/db/base.py       租户过滤基类
domains/opportunities/ scoring.py 的 check_gates 与 compute_score（纯函数）
                       service.py 的实现
```

**为什么选它当第一个域**：它不依赖任何外部服务，打分和状态机是纯逻辑，容易测；而且它是北极星指标的载体。

**验收**：能用脚本插入一条机会 → 跑打分 → 看到分桶和门槛解释 → 状态推进到 lost 时强制要求 `loss_reason`。

**陷阱**：打分快照**只增不改**。重新打分产生新快照，旧的留着——分数的变化历史本身是信息（客户回复补充了证据，分数上去了）。

---

#### 切片 3 · 机会看板与人工接管（第一个可演示版本）

**做什么**：`apps/api` 起来 + 最小前端，员工能看到机会、接管、填写下一步。

```text
apps/api/main.py                装配、租户中间件、错误转换
apps/api/routers/crm.py         机会列表/详情/状态推进/接管队列
domains/employees               归属锁 + 分配解析（八级优先序）
apps/web                        两个页面：机会列表、接管队列
```

**这一步之后系统就有用了**：需求靠人工录入，但机会管理、分配、接管 SLA 已经在跑。

**验收**：手工造 5 条需求 → 系统建出机会 → 按 Territory 分给两个员工 → 接管队列按等待时长排序 → 超时升级通知（先打日志，通知渠道在切片 4）。

**陷阱**：接管队列**按等待时长排，不按分数排**。按分数排会让高分机会不断插队，低分的永远等不到人，最后全部超时流失。

---

#### 切片 4 · 发件身份 + 单封手动发送

**做什么**：第一次引入 `connectors` 和 `tool_gateway`。

```text
domains/sending_identity     预热曲线、认证门禁、滚动窗口、自动熔断
connectors/gmail             发送、幂等、退信解析
tool_gateway                 八段管线（先实现 tenant/permission/suppression/
                             idempotency/rate_limit 五段，其余留桩）
notification_gateway         站内 + 邮件两个渠道
```

**做之前先办一件事**：注册冷开发域名，配好 SPF/DKIM/DMARC，**当天就启动预热**。预热要跑四周，是整个项目里唯一无法用工程手段加速的部分——晚一周注册就晚一周能正常发信。

**验收**：员工在界面点「发一封」→ 过完管线 → 真发出去 → 退信/投诉能回流并影响身份信誉 → 退信率超阈值自动熔断。

**陷阱**：
- 幂等键**必须落库**，不能只放 Redis。Redis 丢键等于重复发送
- 当日发送计数**必须原子递增**。读-改-写在并发下会突破预热上限，那正是预热要防的事
- 抑制名单查询在发送关键路径上，**服务不可用时拒绝发送而不是放行**

---

#### 切片 5 · Campaign 与序列自动化

**做什么**：引入工作流引擎和调度进程。

```text
workflows/engine             Postgres 状态机运行器
workflows/outreach_campaign  五个步骤 handler
apps/scheduler_worker        主循环 + 咨询锁
domains/outreach             Campaign 边界、序列、抑制名单
domains/prospecting          可达性验证门禁、法律依据留痕
```

**验收**：批准一个 Campaign → 入组 20 个已验证联系人 → 三步序列按 wait_days 自动推进 → 有回复立刻停 → 每日额度用完就停 → 暂停 Campaign 后不再新发但仍收回复。

**陷阱**：
- `scheduler_worker` **必须单副本**（或先拿 `pg_advisory_lock`）。重复扫描 = 重复推进 = 重复发送
- `stop_on_reply` 要在**发送前再查一次**，不能只靠回复事件。回复可能在发送前几秒到达而事件还没处理完
- 扫描取批用 `FOR UPDATE SKIP LOCKED`

---

#### 切片 6 · 回复识别

**做什么**：第一次引入大模型。

```text
agent_runtime/base + context_builder + guardrails
agent_runtime/qualification_agent    14 类分类 + 字段提取 + 下一问
workflows/reply_qualification        四个步骤
domains/conversations                消息、分类留痕
tests/evals/replies/*                评估集先建，再写 prompt
```

**顺序很重要：先建评估集，再写 prompt。** 没有评估集就调 prompt，等于拿生产客户做 A/B 测试。每类回复至少 10 个样本，退订和自动回复各 20 个（这两类误判代价最高）。

**验收**：评估集上分类准确率达标 → 退订零漏判 → 自动回复零误停序列 → 提取的字段带 provenance 指向原消息 → 命中接管触发条件能生成完整接管包。

**陷阱**：Guardrails 必须在**落库前**拦，不是落库后清理。模型输出概率值、无证据断言、未审批承诺，一条都不能进库。

---

#### 切片 7 · 需求发现（闭环）

**做什么**：接上链路第一环，系统开始自转。

```text
agent_runtime/demand_intelligence    信号解读、假设生成
agent_runtime/account_discovery      企业与联系人发现
workflows/demand_discovery + account_discovery
connectors/web_search
domains/demand                       四层完整落地
```

**验收**：老板下一句自然语言指令 → 系统解析成提案 → 老板确认 → 自动探索产出信号 → 生成假设 → 过打分门槛 → 找到联系人并验证 → 入组 Campaign → 走完全链路到人工接管。**这就是 Phase 1 完成。**

**陷阱**：每轮探索要有信号数和页面读取数上限。没有上限的探索循环会在一夜之间烧掉预算，且产出的大多是噪音。

**当前实现进度（2026-08-24）**：`domains/prospecting` 已具备企业、联系人、联系方式、
法律依据、租户隔离、可达性验证门禁、原子 outbox 与删除后 hash suppression；Hunter 单
Provider 的固定 host transport、typed 联系人补全/邮箱验证 connector、Tool Gateway
checks/handler、30 天验证缓存与一次性 PII 槽也已通过受控 transport 测试。测试没有使用
真实 Key 或真实网络，Provider score/confidence 会被丢弃，不确定付费结果不会自动重试。

账户发现的持久化 workflow、Hunter Gateway、Campaign 入组接线、API 与 UI 已存在；
Company Playbook 的不可变版本、独立审批、批准后自动激活、运行时装配与 Settings 页面也
已实现。这仍不等于切片 7 或 Phase 1 完成：国家政策包及其生产 composition 尚未实现，
因此生产 `contact.enrich` 必须保持未注册；它们是下一个独立切片。Phase 1 的真实运营验收
尚未完成，不得把当前受控测试误报为已上线的 Hunter 连通能力或完整需求验证闭环。

---

## 四、每次改动的自检

```bash
python3 scripts/check_boundaries.py    # 结构边界（秒级，改完就跑）
pytest tests/unit                       # 纯逻辑
pytest tests/integration                # 带库
pytest tests/evals                      # 仅在改模型/prompt 时
```

`check_boundaries.py` 拦八类问题：反向依赖、跨域导入、碰他域内部、外部 SDK 越界、金额用 float、置信度存数值、事件未注册、缺租户过滤、AGENTS.md 缺失、域结构不全。

**它拦的是架构腐化，不是代码写错。** 后者归测试管。架构腐化的特点是每一步看起来都无害，等发现时已经改不动了——所以要机检，不能靠自觉。

骨架期额外加 `--skeleton` 检查 stub 纯度；开始实现后从 CI 里去掉这个标志。

---

## 五、加功能：四个插件点

系统留了四个口子，加功能**只加文件、不改核心**。如果你发现要改核心管线才能加上某个功能，先停下来——大概率是设计走偏了，或者这个功能该放在别处。

### 加一个外部系统

```text
1. connectors/<name>/AGENTS.md     能力范围、合规约束、密钥归属
2. connectors/<name>/client.py     实现 Connector Protocol
3. manifest 里声明 secret_refs     启动时会校验密钥是否配齐
4. 在 worker 入口注册
```

不改任何现有文件。Connector **不含业务规则**——「什么时候发邮件」在 `domains/outreach`，「怎么调 Gmail」在这里。

### 加一个技能

```text
1. skills/canonical/<skill_id>/manifest.yaml   照 schema.yaml 写全
2. skills/canonical/<skill_id>/prompt.md
3. tests/evals/ 加对应样本，manifest 的 eval_refs 指过去
```

`skill_router` 按 trigger 自动发现。**`eval_refs` 不允许为空**——没有评估样本的技能，prompt 改了无法验证有没有变坏。

### 加一个工具

```text
1. tool_gateway/manifest.py 里注册 ToolManifest
2. tool_gateway/handlers/<tool>.py 实现 handler
```

`checks` 字段显式列出要过哪些 stage。管线不动。

### 加一个通知渠道

```text
1. notification_gateway/channels/<name>.py 实现 NotificationChannel
2. 在 notification_worker 注册
```

### 什么时候必须写 ADR

- 改 `shared/` 里的任何契约（事件字段、Provenance 结构、Money 语义）
- 放宽任何一条硬边界
- 引入新的基础设施（图数据库、Temporal、第二个模型供应商）
- 改变分层或插件点的设计

加新事件、加新技能、加新连接器**不需要** ADR。

---

## 六、二十条陷阱

实现时最容易踩的，按后果严重程度排。前五条会造成不可逆损失。

| # | 陷阱 | 后果 |
|---|---|---|
| 1 | 用主业务域名发冷邮件 | 公司邮箱全废，恢复以月计 |
| 2 | 跳过域名预热 | 新域名一周内被判垃圾源 |
| 3 | 幂等键只放 Redis | 丢键 → 重复发送 → 投诉 |
| 4 | 缺租户过滤 | 跨租户数据泄露，测试环境发现不了 |
| 5 | 用 indicative 价格报价 | 亏本成交，单越大亏越多 |
| 6 | `scheduler_worker` 多副本无锁 | 重复推进状态机 |
| 7 | 计数器读-改-写 | 并发下突破发送上限 |
| 8 | 金额用 float | 成本表累加后与报价对不上 |
| 9 | 存模型输出的置信度 | 虚假精度，诱发无意义比较 |
| 10 | 允许推断晋升为已验证需求 | 核心指标变噪音，信任丢了回不来 |
| 11 | 抑制名单查询降级为「查不到就放行」 | 给退订过的人发信 |
| 12 | 只抑制回信的人不抑制企业 | 换个联系人继续骚扰，法律风险 |
| 13 | 把自动回复当回复 | 错停序列 + 污染回复率统计 |
| 14 | 接管队列按分数排序 | 低分机会永远等不到人，全部超时 |
| 15 | 覆盖已锁定的成本表 | 「上周报价怎么算的」无法回答 |
| 16 | 事件处理器不幂等 | 重复投递 → 重复建机会/重复通知 |
| 17 | 上下文超预算时静默截断 | 模型基于残缺信息做判断，无从排查 |
| 18 | 接管包只发一句「有高意向客户」 | 员工忽略 → 客户流失 → 前面全白干 |
| 19 | 承诺存相对时间（"tomorrow"） | 一周后毫无意义，提醒在错误的日子响 |
| 20 | 改评估集样本去迁就模型 | 移动球门，评估失去意义 |

每条的详细说明在对应模块的 AGENTS.md 和 docstring 里。

---

## 七、与 AI 协作实现

这套骨架是**为 AI 实现设计的**：AGENTS.md 就近生效、接口签名完整、约束写在 docstring 里。但用法有讲究。

### 任务粒度：一次一个域的一个切片

```text
好：「实现 domains/opportunities 的 scoring.py，照 docstring 的规则，
     并在 tests/unit 补齐每条门槛的用例」
差：「把 domains 都实现了」
```

第二种会得到一堆看起来对、细节全错的代码，而且错误互相耦合，审起来比重写还慢。

### 每次任务前，让 AI 读这个链条

```text
AGENTS.md（根）
→ 目标目录的 AGENTS.md
→ domains/demand/（作为标准范例）
→ 目标文件本身的 docstring
→ 相关的 docs/architecture/*.md
```

docstring 里已经写清了「要实现什么、边界是什么、为什么」。**如果 AI 的产出违反了 docstring 里明写的约束，是它没读，不是描述不清**——让它重读再来，不要自己动手改。

### 验收三步

```bash
python3 scripts/check_boundaries.py   # 1. 结构没坏
pytest tests/unit -k <模块>            # 2. 逻辑对
```

3. **人读关键约束**。机器检不出的部分：门槛有没有被偷偷放宽（多了个 `force=True` 参数）、错误消息够不够具体、幂等是不是真的幂等。这三样是 AI 最容易糊弄过去的地方。

### 绝不能交给 AI 决定的

这些是商业判断，不是技术判断：

```text
硬边界的松紧            利润底线与最大折扣
熔断阈值                Playbook 的排除品类与金额底线
预热曲线的激进程度       打分门槛的具体数值
探索配比（70/30 那个）   哪些动作必须人工审批
```

AI 会给出一个「看起来合理」的默认值，而这些数字直接决定烧钱速度和风险敞口。**代码里不该有一个隐含的数字替老板做决定**——这也是为什么 `get_playbook()` 在未配置时抛错而不返回默认值。

---

## 八、Phase 1 完成的标准

不是「代码写完了」，是这四条同时成立：

```text
1. 能从零起一个 Campaign，产出至少一批已验证需求
2. 每条已验证需求都能点开看到客户原话和证据链
3. 发件域名信誉健康（退信率、投诉率在阈值内）
4. 人工接管的等待时长可度量，且没有超时流失的案例
```

达到后先别急着做 Phase 2。**先看三个数**：已验证需求的产出速率、每条的成本、接管响应时长。它们决定 Phase 2 优先自动化哪一段——如果瓶颈是人工接管太慢，那么自动化寻源没有意义。

如果 Phase 1 发现核心假设不成立（信号噪音过高、回复率长期上不去），**要改的是发现与触达策略，不是继续往下游建功能**。这是整个路线图里最重要的一条纪律。

---

## 附：常用命令

```bash
# 环境
docker compose -f infra/docker-compose.yml up -d
cp infra/.env.example .env          # 填值；填好的 .env 永不提交

# 自检
python3 scripts/check_boundaries.py
python3 scripts/check_boundaries.py --skeleton   # 骨架期

# 查文档
docs/architecture/00-overview.md    # 总体架构
docs/adr/                            # 决策与理由
GLOSSARY.md                          # 术语（写代码前对一遍用词）
```
