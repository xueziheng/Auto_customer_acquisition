# TradeOS 实现手册

项目已完成本机受控 Web 核心闭环及若干 Phase 2 切片（2026-09-06）；真实运营和共享部署尚未验收。
目录、边界与契约仍是实现依据，下文早期切片按历史实施顺序保留，当前状态逐项注明。这份手册回答一个问题：**怎么把它变成完整可运营的系统，
且过程中架构不腐化。**

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

先按[本机操作说明](docs/operations/web-core-local.md)建立Python3.12+环境；下文`python3`须指向该环境，
可执行`source .venv/bin/activate`或将命令解释器换成`.venv/bin/python`。系统Python3.9不能解析本仓语法。
然后跑一次自检，确认环境正常：

```bash
python3 scripts/check_boundaries.py
```

### 三个最容易理解错的地方

**「已验证需求」不是状态字段，是门槛。** `NeedHypothesis` 升级为 `ValidatedNeed` 只有一条路：客户本人说过。Agent 推断一百条也不行。整个系统的商业价值取决于这个数字可信——一旦开后门，它就变成噪音，而且信任丢了就回不来了。

**置信度不是模型输出的。** 模型只判断「这条证据属于哪一档」，档位由 `shared/schemas/evidence.py` 的 `derive_confidence` 用规则推出。数据库里没有 `confidence` 数值列，这是有意的。

**Campaign 是授权书不是配置。** 老板批准一个边界（哪些市场、哪些品类、几封信、每天多少），Agent 在边界内自主工作不用逐次请示，越界的动作被 Tool Gateway 拒绝。改边界 = 新版本 + 重新审批。

---

## 二、第 0 步：补齐工程基建

本节是早期基建实施说明，现有 pyproject、迁移、Repository、测试、Makefile 和 CI 已落地。
本机 Web 的真实启动命令与依赖来源见第十四节，不再按缺基建处理。

### 已落地的基建清单

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

原基建验收入口为`make dev`、`make test`与`make check`；当前测试已包含真实业务作用组，
不再是空测试。最终实际命令、版本与结果见第十四节。

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

**当前状态（2026-09-06）**：公共契约已实现，Decimal/Provenance/证据等级边界已回归。
以下保留该切片原实现目标与验收要求。

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

**当前状态（2026-09-06）**：持久化与机会域已实现；真实合格机会产出不由合成数量推导。
以下保留该切片原实现目标与验收要求。

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

**当前状态（2026-09-06）**：Web 队列/证据包/接受已贯通；当前角色与归属在 API 重新授权。
以下保留该切片原实现目标与验收要求。

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

**当前状态（2026-09-06）**：原 Gateway、发件身份和受控 Gmail 链已实现；真实 DNS/发件信誉/邮件外发未验收。
以下保留该切片原实现目标与验收要求。

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

**当前状态（2026-09-06）**：原持锁 scheduler 与获批 Campaign 已接通；未验证联系人拒绝入组。
以下保留该切片原实现目标与验收要求。

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

**当前状态（2026-09-06）**：正文入站→不可变原件→分类/逐字段证据→Need/Opportunity/Handoff 已受控验收。
以下保留该切片原实现目标与验收要求。

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

**当前状态（2026-09-06）**：原 typed research_only 已接线并执行；只到 Signal/Hypothesis，触达需另行明确批准。
以下保留该切片原实现目标与验收要求。

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
已实现。国家政策包现已实现 tenant-scoped 不可变版本、逐字段人工确认 Provenance、独立
审批、批准后激活、Settings 管理、结构化 fail-closed Gateway reader 与真实就绪原因。

Hunter Provider 的安全配置声明、tenant-scoped append-only readiness、`/account` 人工验证
插件、双工具条件注册、scheduler 单例锁后激活与 Settings 持久事实展示已经实现。缺少配置、
当前配置未验证或结果失败/不确定时，`contact.enrich` 与 `contact.verify` 均不注册；验证通过的
候选进程才同时构造两者，但只有锁 owner 写入 matching `runtime_composed` 后才可进入业务
cycle。配置漂移或 reader 故障仍会在凭证解析和 Provider IO 前失败。

这仍不等于切片 7 或 Phase 1 完成。仓库验收没有配置真实 Hunter Key、没有访问真实 Hunter
网络，真实 Provider validation/smoke 均为 `not_run`；生产部署是否已激活也没有外部事实。
后续运维必须按 `docs/operations/hunter-provider-readiness.md` 逐次声明、真人验证和重启单例
scheduler。Phase 1 仍需真实 Campaign、客户原话与 Provenance 证据链、健康发件信誉和已测量
的人工接管 SLA；这些运营验收当前为 `not_run`。

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

这是原运营推进准则；当前 Phase 2 多个工程切片已受控交付，但真实运营并未达标。生产推广前仍须**先看三个数**：已验证需求的产出速率、每条的成本、接管响应时长。它们决定 Phase 2 优先自动化哪一段——如果瓶颈是人工接管太慢，那么自动化寻源没有意义。

如果 Phase 1 发现核心假设不成立（信号噪音过高、回复率长期上不去），**要改的是发现与触达策略，不是继续往下游建功能**。这是整个路线图里最重要的一条纪律。

---

## 九、Phase 2 首批：免费来源获客实施与验收

本批已完成工程实现、受控验收与独立审查，并按用户选择合并到本地 `main`；**真实联网仍未运行，未推送或部署**。不改变第八节的 Phase 1 真实运营完成标准，也不代表整个 Phase 2 已完成。原有寻源、成本、报价自动化保留在 Phase 2 后续交付中。

规格与执行清单：

- `docs/superpowers/specs/2026-08-27-phase2-free-discovery.md`
- `docs/superpowers/plans/2026-08-27-phase2-free-discovery.md`
- `docs/adr/0016-free-search-provider-contract.md`

### 实施顺序

1. **契约与额度保护**：增加 Tavily 连接器和供应商无关接口；固定 basic，禁止自动升级；先查 `/usage` 再持久预留，再进行搜索。并发和进程重启都不得重复使用同一份额度。Brave 原有行为保留，免费路径不回退 Brave 或商业来源。
2. **三线路与研究工作流**：老板明确国家、品类、排除项、预算，确认提案后执行 importer / distributor / ecommerce 三类查询；新查询写明线路。新版本支持 `research_only`，旧提案缺省仍按原触达准备语义解释，保留旧工作流定义。
3. **原网页证据与企业核验**：搜索摘要仅定位 URL；读取原网页、保存 URL/时间/哈希/不可变快照及字段级 Provenance。目录网站不是其所列企业的官网；缺官网或所在地证据的结果待核验，不强建企业或假设。跨线路复用已有企业消歧并保留多份证据。
4. **界面接线**：指挥中心确认前展示只研究模式、三线路、市场与预算；需求雷达、客户发现、Run Center 展示证据、核验状态、额度消耗和停止原因。前端类型必须从 OpenAPI 生成。
5. **整体验收**：每个切片先失败测试再实现，独立审查后做全量回归和浏览器验收。交付报告分开记录真实联网、受控结果和未运行项。

### 启用条件与停止语义

用户自行配置免费账户的**密钥引用**及已确认研究预算，不在对话、提案、日志或事件中传递密钥值。公开研究仍须通过已有租户、权限、Playbook 和国家政策检查，免费账户不构成来源访问或触达授权。

没有免费账户配置可以完成受控测试，但真实 Tavily 搜索必须标为 `not_run`。不得用模拟结果代替真实联网，也不得把搜索额度免费解释为模型和基础设施没有成本。

免费计划或按量计费状态未知、付费开启、免费额度不足、结果不确定分别保持可辨识的停止原因；来源不支持、页面禁止访问、真正没有结果也不能统一显示为“没有买家”。不自动升套餐、清除未决预留或重试可能已执行的请求。无可信账期时不得凭本机月份变化恢复额度。

### 本批验收清单

- 三线路都能从确认提案形成可追溯候选、Signal 和 Hypothesis；跨国家/跨线路同企业不重复建档。
- 摘要、企业自述、技术特征不会被升级成运输记录、客户明确需求或已验证邮箱；缺证据的企业保持待核验。
- 并发最后额度、计费开启、usage 读取失败、timeout、429、取消与重启恢复均不会触发重复调用或付费回退。
- `research_only` 的联系人补全、邮箱验证、发信和报价调用数均为零；旧流程审批、验证和发送门禁仍有效。
- 私网、越界重定向、登录墙、验证码、禁止访问页面均被阻断；凭证和原始敏感内容不进入日志或事件。
- 三类公开页面的真实读取结果，与受控联系人验证、发送、回复识别、需求验证、接管结果分栏，未运行项明确标注。
- 迁移在隔离测试库执行 upgrade → downgrade → upgrade；所有新增表/查询隔离租户。
- 完成结构检查、后端相关与全量回归、前端测试/类型检查/构建、OpenAPI 无漂移、浏览器验收与独立审查。

以上是验收要求，不是已经通过的结论；完成后以本批验收报告中的实际命令、结果和证据为准。未接入的商业贸易/目录/电商服务只保留插件扩展边界，不显示为可用。

### 配置、真实来源验收与停止（2026-08-28）

1. 运维在进程环境自行配置 `TAVILY_API_KEY_REF`（环境变量名引用）及其指向的密钥，
   并且仅在账户确实独占时设置 `TRADEOS_TAVILY_EXCLUSIVE_ACCOUNT_CONFIRMED=true`。
   不在聊天、提案、命令参数或报告中粘贴密钥。账户不得跨部署共享；换引用/换租户不能恢复额度。
2. API只读取这两项安全配置声明。`configured_unverified` 不代表账户已核实、余额准确或
   scheduler已启用。缺配置时后端拒绝研究确认；旧outreach_preparation不受此新配置影响。
3. 老板在指挥中心核对并确认仍为当前active的 `research_only` 提案，包含三线路、国家/品类、
   排除项与查询/页面/信号/假设预算。操作者须仍是在职且实际确认该提案的老板。
   真正生效的Playbook与精确国家政策必须通过原审批流程配置，验收脚本不会代建许可。
4. 来源验收复用原部署Postgres和原账户quota；0040是本节当时的历史迁移状态，当前checkout运行前必须满足其合法单head，不能把0040当永久版本要求。不可另起空库规避历史预留。
   `DATABASE_URL`、tenant、HMAC key引用/版本、工具lease、S3配置与原始资料大小上限仍须齐全。
   用户自行加载部署环境，脚本不自动读取 `.env`，不自动迁移。不要展示环境或DSN。
5. 先停止普通scheduler推进，避免这次API确认产生的正常业务Run与来源验收同时耗用研究预算；
   另行确认这次来源验收自身的预算消耗。下列第一条是安全默认，第二条才会访问真实来源：

```bash
PYTHONPATH="$PWD" python3 scripts/accept_research_discovery.py
PYTHONPATH="$PWD" python3 scripts/accept_research_discovery.py \
  --live --budget-confirmed --proposal-id "dpr_REPLACE" --actor-id "emp_REPLACE"
```

专用流程 `research_source_acceptance` 只在脚本引擎中注册；正常scheduler不会领取它。
其真实装配是 TavilySearchApiTransport → 原Tool Gateway/持久额度 → SafePublicPageHttpTransport
（SSRF/robots/访问墙检查）→ S3 RawArtifactStore（Postgres元数据），不是factory占位。
页面只能来自已批准查询的搜索结果，不能以任意URL旁路读取。Source URL、UTC观察时间、hash、
artifact引用和线路保留在专用Run，原始HTML留在Artifact Store，不向模型提供凭证。
该入口无模型、联系人补全/验证、Campaign、发信或报价端口；输出固定 `scope=pages_only`、
`model=not_run`、`outreach=not_run`，不得将它的完成计入Signal/Hypothesis成果。

标准输出为JSON。缺显式opt-in/预算/配置时是 `not_run`；生命周期 `completed` 只表示
此次来源检查结束，须同时检查 `reason=pages_only` 与三线路实际页面证据才算来源验收成功。
`no_results`、`partial_sources`、预算不足和政策/Provider拒绝不当作“没有买家”或验收成功。
`searches_used/pages_used` 是尝试计数；`consumed_credits/reserved_credits/uncertain_credits`
按tenant+Run读持久quota，不能拿账户累计预留或网页数当本轮实际credits。

`not_run`只用于可证明本次尚未进入执行的拒绝。调用真实入口后未分类的异常（包括结果查询、
连接中断、资源关闭失败）输出 `status=unknown, reason=execution_status_unknown`，exit3；
这不证明未消耗额度。若engine已返回Run ID，输出经过格式校验的`run_id`，不从异常文字猜测。
遇到未知状态不得自动重试、清表或换幂等键，先按tenant+Run及原提案人工核对Run和持久ledger。

脚本同提案使用固定幂等键。再次执行不会创建新的预算槽；不确定搜索不自动重试、退款或
释放预留。用Ctrl-C停止，尽力写入取消终态；强制杀进程可能留下专用在途Run，普通scheduler
仍不能接走。运维须核实ledger和原提案后再决定是否同键恢复，不得清表重跑。
完整正常研究worker仍需显式 `DemandDiscoveryComposition` 与真实模型能力装配，
仅设置API变量不完成该工作；本次没有实际模型研究启动或生产激活证据。

兼容与验收：历史未带mode/lane的提案继续解释为outreach_preparation，保留v1/v2流程。
受控三线路Signal/Hypothesis与受控旧联系人→验证→发送→回复→已验证需求→接管分别运行；
真实来源不会执行下游。采用 `pytest -m "not e2e"` 和 `pytest -m e2e` 分开验收。
最终记录见[本批验收报告](docs/acceptance/2026-08-27-phase2-free-discovery.md)，Phase1真实运营仍not_run。

### 实际装配中的锁边界

真实多连接验收复现：engine在handler期间持Run行锁，旧Web预算guard另开连接再取同一行锁，
导致3秒测试超时且Provider零调用。此前使用假search的Postgres研究测试不覆盖这个组合缝隙。
现用独立 `tradeos:web-run-budget:v1` tenant+run advisory transaction lock串行预算读取；
不改engine/Gateway核心，保留type/status/execute_search/tenant/预算校验与ledger保守计数。
锁只消除自等待，不扩大预算；两个并发最后预算请求最多一个dispatch，可能保守双拒绝。

---

## 十、Phase2成本与报价批次

本批验收记录见[成本报价验收](docs/acceptance/2026-08-28-phase2-costing-quotation.md)，
启用/停止及未知结果恢复见[运维说明](docs/operations/costing-quotation.md)。工程实现、受控验收和全分支独立审查已完成；尚未合并、推送、部署或启用真实外部服务。仅本批范围，不代表整个Phase2完成，不改变第八节Phase1真实运营标准。

1. 按当前合法单head迁移，API/worker分别显式配置报价core、evidence和可选files预算，实际Linux解析能力probe通过才启用。旧部署未被测试升级。
2. 客户原话真实验证为Need，机会当前归属明确；具有来源权的人确认单位。老板政策、供应商quoted依据、22项费用适用性与来源映射分别留痕，禁止把缺失当零。
3. 用户明确利润政策、数量档、正向汇率、舍入精度及期限；Decimal计算并冻结成本basis，独立审批人批准精确版本。没有默认业务阈值，批准不发送。
4. 通过独立客户文件授权生成/下载PDF，深链核对quote ID。未知结果保留原键/调用ID，先查账，再按原绑定恢复，禁止重生成或自动换键。
5. 执行结构/静态、后端非E2E和E2E、前端test/typecheck/lint/build及API生成无漂移门。隔离PG/受控transport、Browser、真实供应商和真实发送分栏，不以测试替运营。

## 十一、Phase2 Sourcing Case V2 子项目

本子项目把完整度为 3 的已验证 Need 推进为受控公开寻源、候选产品卡、人工审核和单一
`ESTIMATED` 成本交接；它不等于整个 Phase2 完成。配置只保存
`TRADEOS_SOURCING_SETTINGS_JSON` 中的 Tavily secret ref、model identifier、查询/页面硬上限和
系统 actor，绝不写入密钥值。构造 scheduler 不解析 Tavily ref、不联网；运行时一律经 Tool Gateway。

操作顺序是：记录内部 rung 1–5 → boss 草拟计划 → 以当前 plan hash 确认 → 检查免费额度后 run。
替换计划令旧确认失效。额度 unknown、paid、exhausted 或 external request uncertain 一律停止；
unknown/paid 不得“先试一次”，uncertain 保留预留并由 boss 人工核对后按原 Run/请求键恢复。页面禁止、
验证码、不安全重定向和无结果都保留不同 stop reason，不能归并成无供应。

公开页面只生成带 URL/时间/hash/Artifact 的 immutable calibration draft。Candidate 的公开
`product_type`/`size` 与内部 Product `product_category`/`size_spec` 是不同词表；Need `model` 可选，
未知不得补造。Case 的 `need_snapshot` 是唯一 canonical 规格来源：Need 已声明的 product type、material、
size、application（和已声明时的 model）都必须由 Candidate 复述为相同 required value；声明的 model
不得省略或漂移，未声明时也不得把 model 设为必填。完整却不合格的结果保留为 rejected Candidate，不能删除改写草稿。公开价格均为
`INDICATIVE`，不得新写 quoted price、发供应商询价或客户 Quote。

候选封存后由 `SourcingCandidatesVerified` 投影为 `source_only` Product/Supply Option；完整卡集才发布
Ready。sourcing 人员先提交 primary（可选最多两个 alternate），boss 再确认同一事实。仅当有 Opportunity
才发布 handoff；缺失时持久化停在 `opportunity_required`，补齐后只能以同一 Run 的精确 retry 恢复，并且只创建一个 ESTIMATED CostSheet，
其中 `product_purchase` 使用 Decimal 与 indicative basis。没有任何一项授权联系人发现、邮箱验证、发信、
采购或真实 direct supplier quote。

受控验收使用真实 PostgreSQL migrations、领域服务、V2 Workflow、Tool Gateway、Outbox 和 API；可替换的
仅是 Tavily、public-page 和 extraction-model 外部端口。真实 Tavily 需要用户给出明确国家、品类和搜索/页面
预算的额外授权。本轮未给出时固定记录为：

```yaml
real_tavily_supplier_pages: not_run
reason: explicit_market_category_and_research_budget_not_provided
```

不要读取、打印或复制密钥值；“环境已配置”不构成真实联网授权。完整门禁和 Browser 数据态证据以
`docs/acceptance/2026-08-30-phase2-sourcing-case-product-cards.md` 的实际结果为准。

Linux 来源门的 dependency-only 基础产物也有明确的网络边界。开发人员需要更新该受审计产物时，先从
项目 Python 环境显式运行下列 bootstrap；它只使用固定官方 Python digest 和仓库
`tests/fixtures/quote_evidence/linux/requirements.lock` 中带 SHA-256 的公开 Linux/arm64 wheels，不能包含
`tradeos-agent` 或 `file://` 项：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  python -c 'from tests.integration.quote_evidence_linux_support import bootstrap_dependency_image; print(bootstrap_dependency_image())'
```

bootstrap 是此门唯一允许访问公开包仓库的阶段；它写入带固定 lock hash 的 **lock-addressed bootstrap
tag**，其后必须作为已验证的 dependency-only artifact 复核，而不是宣称为不可变 content address。标签标记
base digest、lock hash、pip manifest 与 `linux/arm64`。正式 `image_id()` 和 Linux pytest 不会自动 bootstrap、
pull 或访问 PyPI：只按该精确标签读取，复核 labels、候选 RootFS 是否以本地 pinned Python digest 的真实层序列
为前缀、pip manifest、必需模块，以及 `/opt/tradeos`、`tradeos-agent` 和所有 TradeOS 顶层源码根/包均缺席；
找不到 artifact 时 hard-fail 并要求先显式运行 bootstrap。之后当前源码层和所有 runner 构建一律
`network_mode=none`，source manifest 覆盖其白名单输入。

## 十二、Phase2 NeedCluster Sourcing Admission

本子项目只控制“哪一个已存在的 Sourcing Case V2 可以启动 Workflow”。它已完成工程实现、受控核心和
Browser 验收；尚未合并、推送、部署或生产启用，不代表整个 Phase2 完成。一个 `Validated Need` 始终
对应一个 Case 和一份独立 Need snapshot；需求簇不是合并订单，也不会合并数量、规格或 Provenance。

### 正常操作

1. 先按当前合法单 head 完成迁移，并核验 API 与 scheduler 使用同一新版构建；0055 的 Run guard、
   subject 唯一索引及 0056 的人工请求恢复身份保护必须已生效。生产升级、历史回填和回退的完整窗口要求见
   `docs/operations/sourcing-admission.md`。
2. 完整度达到 3 的 Need 只会经真实 Outbox 建立 canonical V2 Case 和 durable Admission。没有当前
   boss-confirmed `sourcing_admission` Directive、策略关闭或策略状态未知时，scheduler 必须保持零
   Workflow start；不得直接写 active Directive、Admission 或 Run 表绕过确认。
3. 老板在指挥中心创建准入提案，逐项核对 `mode=cluster_ranked`、自动准入开关和整数
   `batch_limit`，再确认精确提案。确认只更新策略，不会在 API 请求内领取 Admission 或启动 Run。
4. scheduler 每轮先读取当前策略，再释放已到期租约并按“需求簇成员数降序、`ready_at` 升序、稳定
   ID”领取不超过 `batch_limit` 条。每条仍独立启动并绑定 canonical Run；进入 `starting/admitted`
   后不因新的 membership 事件改写排序快照。
5. 在寻源中心分别核对“等待准入”和“处理中”，并打开准入审计详情查看 immutable snapshot、事实观测
   时间、排序版本、Directive 版本、准入人和时间。成员数只表示当前有多少条相似已验证需求，不表示
   集体采购或供应已经确认。
6. 授权老板或 sourcing 员工可逐条人工准入。每次使用一个非空原始 `Idempotency-Key`；若 HTTP 结果
   不确定，先 GET 当前 Admission/Run，再由**同一个 actor 以同一个键**重放。原键与可轮换的 scheduler
   租约 token 分离持久；禁止换 actor/key 模糊原命令。0056 前 actor-only 历史行不能人工猜键恢复。

### 停止

- 计划性停止：老板提交并确认 `automatic_admission_enabled=false` 的新提案；下一轮起不再 claim 新项。
  这不会取消已经 admitted 的 Run，也不删除 Waiting Admission 或不可变快照。
- 紧急停止：先停止 scheduler worker，再核对 `waiting/starting/admitted/blocked` 数量、未到期 claim 和
  Workflow Run。不要靠删除 Admission、清快照、改租约时间或 downgrade 来假装已经停止。
- 策略读取失败、返回未知结构或存储结果不确定时必须失败关闭为零新 start。日志和 UI 只显示固定停止
  原因，不保存原异常、Need snapshot 或 Provenance 原文。
- 准入只授权 Workflow start，不授权 Tavily/页面/模型、联系人、邮件、采购、供应商询价或客户 Quote；
  下游仍各自受现有计划、额度、Tool Gateway 和人工审批门禁控制。

### 重启与恢复

1. runtime 重建后从 PostgreSQL 读取当前策略、Admission、租约和 Run；canonical
   `sourcing-case:v2:{tenant}:{need_id}` 业务键、数据库 guard 与 subject 唯一索引共同阻止重复 Case/Run。
2. `starting` 且已有 canonical Run 的记录按原绑定完成恢复；没有可核实结果的在途项保持等待人工核对，
   到期租约只经正式 release 路径回到 waiting。不得直接把 `starting` 改为 `admitted`。
3. readiness 重放只幂等确保同一 Need 的 Case/Admission；`NeedClusterMembershipChanged` 重新读取 Demand
   当前事实并只刷新 matching waiting/blocked 项。`NeedClusterFormed` 保留“第二成员首次形成多成员簇”
   的既有语义，不由准入路径消费。
4. 历史 OPENED V2 Case 缺 Admission 时，先停 scheduler/人工准入并运行默认 dry-run：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  PYTHONPATH="$PWD" python3 scripts/backfill_sourcing_admissions.py \
  --dry-run --tenant-id "tenant_REPLACE"
```

只有核对变更窗口、备份和精确 tenant 后才可显式 `--apply`。回填只补 Admission 和首份 immutable
snapshot，不启动 Workflow；恢复 scheduler 前再次核对 active Directive、重复 V2 subject 数为零及所有
结果的固定原因。

受控验收的真实命令、`8-member / 3-member / unclustered` 排序、batch 2、runtime 重建、人工重放、
外部调用零计数和 Browser 证据见
`docs/acceptance/2026-09-02-phase2-need-cluster-sourcing-admission.md`。联系人多源瀑布、70/30 allocator、自动
backpressure、真实 direct supplier quote 与商业来源仍未完成。

## 十三、Phase2 Catalog Product Proposal

本子项目已完成工程实现、受控 PostgreSQL/API/scheduler/Browser 功能链、390px 元素级视觉验收与
全仓门禁；它只把通过
显式策略的 Need Cluster 变成内部 Catalog Product Proposal，人工批准后最多创建一个
`queued` 培养 Case。这不是正式 Product、供应确认、客户报价、生产启用或市场验证，也不代表
整个 Phase2 完成。

### 受控运行

1. 先核对已迁移的 PostgreSQL、API 和 scheduler 来自同一构建，并确认当前没有未经批准的生产
   Catalog 策略。无活动策略是正常关闭态，scheduler 不得创建 evaluation、proposal 或 Case。
2. product/sourcing 人员只提交完整显式门槛；系统不提供国家、品类、客户数、复购或数量的
   业务默认。由不同的 boss 打开精确 Approval 深链，核对策略内容、基线版本和影响后再决定。
3. scheduler 只在单副本 advisory lock 下恢复待处理策略/提案、重读 Demand canonical 事实并启动
   durable workflow。确定性规则展示六种固定结果；未配硬门槛不等于事实已知，缺失复购、数量或单位
   仍显示“未知”，但按当前显式策略可不阻断。
4. boss 对提案的批准只入培养队列。操作后同时核对 proposal、Approval 与 Case 的精确绑定，以及
   Product、Supplier、Contact、Search、Send、Quote 和 Tool Gateway 外部调用均未增加。

### 停止

- 计划停止：不再提交新策略；需改变现行门槛时必须提交新版本并重走独立审批，不得直接改
  active 行。
- 紧急停止：先停 scheduler worker，再核对 pending policy/proposal、running workflow、pending/dead Outbox 和
  queued Case。不删行、不改 hash、不补写审批状态、不用 downgrade 伪造恢复。
- 停止 scheduler 不撤销已生效策略或已排队 Case。若需改变这些业务事实，必须另行设计可审计命令和
  审批；本子项目没有紧急“删库开关”。

### 重启与恢复

1. 重建 runtime 后从 PostgreSQL 重读 checkpoint、活动策略、待提交 proposal 和 workflow；以 tenant +
   subject + facts hash 的幂等键恢复，不能用新 actor/key 制造第二份业务事实。
2. Approval 投递、决定或 Case 创建结果不确定时，先读 canonical proposal/Approval/Case，再以原事件或原
   workflow 恢复。同一决定重投和 scheduler 重启只能保留一份 evaluation、proposal、Approval 和 Case。
3. 应用提案决定前重读当前策略与 Demand 事实。facts hash 已改的旧快照必须转 `stale`，不得因
   已点“批准”而创建第二个 Case。
4. `CatalogCultivationQueued` 目前没有本子项目内的下游消费者；培养后续必须另行规格、授权和装配，
   不得把 no-handler dead 当成已执行培养。受控结果、命令、截图和完整残余风险见
   `docs/acceptance/2026-09-04-phase2-catalog-product-proposal.md`。

## 十四、Web 核心交付（2026-09-06）

四进程原入口、安装、开发身份、研究/触达的独立授权、重启/停止和备份恢复见
[本机操作说明](docs/operations/web-core-local.md)。页面到真实执行者及未配置项见
[能力矩阵](docs/operations/web-core-capability-matrix.md)。[验收](docs/acceptance/2026-09-05-web-core-completion.md)
分列48e4465完整9318通过与7e10383修复后主链1通过；Web411通过，不累计局部数字。

- HUP重启API/scheduler/notification/Web，保留同owner数据。PG单独stop/start可能改随机HostPort，
  不能沿旧配置假称透明恢复；已验短断是同端点pause/unpause。
- 备份恢复只证明静止owned PG+对象原件到另一owned空目标的metadata/SHA256一致；不覆盖运行库，
  不恢复受控邮箱/模型状态，也不是生产备份产品。完整停止后新启动是新环境。
- Mac原入口无完整quotation/自动寻源准入；独立Linux公开回复→来源/单位/Decimal成本→独立审批/PDF
  已验，不能用不同owner/Need拼成Mac完整报价入口。Catalog培养终点仍queued。
- Task1/2为通用技能/上下文/权限交集组件验证；生产Agent任务源、policy/descriptor/模型消费者仍未装配。
  Browser无生产任务来源仍disabled；当前研究公开页通过原Gateway，不依赖桌面登录态。
- 真实Provider/供应商/邮件、多人认证、TLS/共享部署、实际计费token和人工工时未验收。
  单位合格贸易机会成本暂无可靠分母/成本来源，不报告0或宣称效率改善。

[桌面扩展契约](docs/architecture/12-client-capability-boundaries.md)只保存真实接口与未来适配责任；
没有Tauri或假IPC。后续共享部署须验证后端会话、员工映射、撤销、CSRF/来源限制、TLS、迁移、
备份保留/恢复、单副本scheduler和告警，禁用dev身份；逐次审批及真实外部调用授权仍适用。
[正式证据与裁定索引](docs/acceptance/web-core-delivery/README.md)保留历史失败、取舍与成本；
最终全分支review由控制者追加，未发生的审查不填通过。

## 十五、本机持久 Web 内测（2026-09-07）

真实账号、生产Web构建、三应用与独立持久PG/MinIO现由`scripts/run_web_pilot.py`提供。首次使用
必须由操作者显式提供业务政策，再依次`init`、`start`和在
真实TTY中创建账号；系统没有默认员工、默认密码、默认市场或测试业务政策。完整命令、政策字段形状、
`getpass`交互、私有权限与冷备份恢复步骤见
[持久内测操作说明](docs/operations/web-internal-pilot.md)。

- 当前schema为单一head `0060`；`start`只核验不迁移。生产Web须先执行`gen:api`和`build`，页面与
  `/api`同源，不使用开发员工头。
- `stop`只停止当前profile精确owner的应用和存储，保留profile、容器及卷。关机前必须stop并核对
  应用/存储均stopped；不要用prune、宽泛pkill或删卷代替。
- `disable`和`reset-password`撤销旧会话；恢复副本也撤销全部会话。多标签登录/退出要求Web Locks，
  当前实际浏览器仅Chromium 151.0.7922.34。
- 冷备份要求全部应用和存储已停止，只恢复到全新profile/新owner。备份含敏感配置和业务数据，目录
  0700、文件0600；当前无加密、自动保留或异地复制，与原盘同故障域时不能抵御磁盘损坏。
- 生产源`ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242`、测试修订
  `63150da376b07414088f5c0f90bcb7ea5f22e075`的增强E2E为1 passed/34.15s；覆盖刷新、完整stop/start、
  DB/对象hash、退出失败重试与旧token重放、直接跨标签换号/共同失效、真实响应交付顺序与Web Lock
  排队、账号停用/重置、冷恢复、重登与390px。console collector的宽泛过滤局限见
  [持久内测验收](docs/acceptance/2026-09-07-web-internal-pilot.md)，不得据此宣称过滤窗口内无其他告警。
- 这是本机loopback内测。共享TLS/反向代理、其他浏览器、真实Provider/供应商/邮件、桌面端与生产
  Browser任务源仍未验收；[桌面扩展契约](docs/architecture/12-client-capability-boundaries.md)继续保留。

受控四应用/Vite入口、持久三应用/生产Web入口和独立Linux报价证据必须分开阅读，不能把不同owner、
不同Need/Opportunity或合成结果拼成一条真实业务闭环。[能力矩阵](docs/operations/web-core-capability-matrix.md)
已按这三类证据分别标注。

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
