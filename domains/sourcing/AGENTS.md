# domains/sourcing/ —— 寻源域

## 职责

Sourcing Case：为目录外需求寻找供应商的完整过程与证据集合。

**Phase 1 的寻源由人工执行**，但数据结构、状态机、核验规则、证据要求全部现在建好——否则 Phase 2 自动化时历史数据格式对不上，且人工阶段积累的案例正是自动化的训练素材。

## 匹配梯子

需求验证后按固定顺序找供应，**不跳级**：

```text
1  正式产品完全匹配
2  正式产品可修改后匹配
3  候选产品匹配
4  现有供应商有类似产品
5  供应商可定制
6  公开寻源（本域的主战场）
7  寻找全新工厂
```

前面的梯级供货确定性高、成本低。跳到公开寻源之前，先确认前五级都不行——这个检查顺序本身要写进 `MatchLadder` 接口。

## 匹配结果必须可解释

**禁止只输出「相似度 87%」。** 那个数字对销售毫无用处——他没法拿它跟客户说任何话。匹配结果必须回答：

```text
哪些规格完全匹配        哪些规格不同
哪些字段未知            是否需要客户确认
是否可以替代            替代会带来什么影响
```

## 状态机

```text
opened ──→ discovering ──→ verifying ──→ candidates_ready ──→ handed_to_costing
    │            │              │
    └────────────┴──────────────┴──→ failed（带原因）
```

`failed` 的原因回流到机会域的 `NO_SUPPLY_FOUND`，是探索策略的反馈信号。

## 候选核验清单（拒绝诱导价）

每个候选供应商必须逐项核对：

```text
产品类型    材质    尺寸    型号    数量档    MOQ    计价单位    币种
```

**拒绝两类价格**：诱导性最低价（远低于市场且无对应数量档的钓鱼价）、模糊区间（"$1–$10"）。用钓鱼价算成本，真实下单时利润直接消失——这是亏本报价最常见的来源。

**最多保留三个合格候选。** 更多候选不会改善决策，只会拖延决策并抬高核验成本。

## 证据快照

每个候选必须保存：网页快照、截图、观察时间、内容哈希。没有哈希，页面改版后你无法证明报价时看到的是什么——对内无法复盘，对供应商无法对质。

## 图片权属规则（硬性）

```text
未知权属图片      不删除水印
未授权图片        不删除 Logo
仅可裁剪          网页 UI、空白、无关背景
授权图片          才可做受控生成式修复
AI 参考图         必须明确标注
处理后            必须检查结构、型号、颜色未被改变
```

最后一条最容易被忽略：修图改变了产品实际结构或颜色，发给客户就构成误导——哪怕技术上只是「美化」。

## 价格基准

**本域产出的所有价格都是 `INDICATIVE`**（硬边界 7 在系统中的入口就在这里）。候选价格只用于内部判断方向；供应商针对具体规格数量实报价后，才由 costing 域升级为 `QUOTED`。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*、任何外部 SDK
```

网页抓取由 `connectors/` 经 `tool_gateway` 执行，结果传入本域。

## 发布 / 订阅

发布：`SourcingCaseOpened`、`SourcingCandidatesVerified`、`SourcingCandidatesReady`、`SourcingCaseHandedToCosting`；旧 `SourcingCaseCompleted` 只作兼容读取。
订阅：无（启动由 Workflow/Outbox 驱动）

## Phase 1 范围

状态机、核验清单、证据快照结构、匹配梯子接口、三候选上限。人工录入走同一套结构。

不做：自动寻源执行（Phase 2）、按需求簇排序寻源队列（Phase 2 挂载点）、1688/以图搜款集成（Phase 2）。

## V2 公开寻源边界

V2 先记录 rung 1–5 的精确证据，再允许 boss 草拟、确认并运行带 hash 的 Tavily basic 公开计划。
额度 unknown、paid、不足或请求 uncertain 必须停止；uncertain 仅能由人工保守核对后恢复。公开
Candidate 使用 `product_type`/`size`，内部 Product 使用 `product_category`/`size_spec`；两套词表不得
互推。模型输出是 immutable calibration draft，完整但不合格的候选必须保留为 rejected Candidate。
所有公开价格保持 `INDICATIVE`，本域禁止写入新的 quoted 价格。审核先 submit，再由 boss 对同一
primary（最多两个 alternate）确认；Opportunity 缺失时停止 `opportunity_required`，不得交接成本。
