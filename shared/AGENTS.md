# shared/ —— 公共契约层

## 职责

定义**跨模块共享的契约**：领域事件、Provenance、金额、强类型 ID、全局错误。

## 最重要的一条

**`shared/` 是叶子节点，不导入任何上层模块。**

```text
允许   shared 被所有人导入
禁止   shared 导入 domains / workflows / agent_runtime / apps / connectors
```

违反这条会立刻产生循环依赖，而且很难拆。

## 第二条

**`shared/` 不含业务规则。**

它定义「金额长什么样」，不定义「什么价格算低利润」；定义「事件长什么样」，不定义「什么时候发这个事件」。业务判断属于对应的域。

判断方法：如果一段代码需要知道贸易业务的具体规则才能写，它就不该在这里。

## 为什么需要这一层

设计稿原本没有公共层。但 16 个域如果各自定义 Money、Provenance、事件类型，必然发散成 16 套不兼容的定义——届时跨域协作要写转换代码，Provenance 也会因为字段不统一而无法追溯。

这一层是解耦的前提，不是额外的抽象。

## 结构

```text
shared/
├── events/
│   ├── bus.py           EventBus Protocol —— 域间通信的唯一通道
│   └── catalog.py       全部领域事件定义（事件即契约）
├── schemas/
│   ├── provenance.py    来源追踪（硬边界 4）
│   ├── evidence.py      证据等级与置信度推导（硬边界 3）
│   ├── money.py         Money / FxRate，禁止 float（硬边界 2）
│   └── identifiers.py   强类型 ID
└── errors.py            全局错误基类
```

## 改动约束

`shared/` 的每一个改动都是**公共 API 变更**：

- 改事件字段 → 所有订阅方受影响 → **必须留 ADR**
- 改 Provenance 结构 → 影响历史数据可读性 → **必须留 ADR**
- 加新事件 → 不影响现有订阅方 → 不需要 ADR，但要更新 catalog 的文档

改之前先问：这真的是所有人都需要的，还是只有一个域需要？只有一个域需要的东西放到那个域里去。

## 命名约定

- 事件用**过去式**：`NeedValidated`、`OpportunityQualified`、`QuoteApproved`。事件描述已经发生的事实，不是命令。
- ID 类型用 `XxxId`，全部是 `NewType`，不用裸 `str`——裸 `str` 会导致把 `need_id` 传进要 `opportunity_id` 的参数而类型检查不报错。

ADR0026新增email_inbound frozen DTO/Protocol：Provider含进程内原件，Archived仅必要关联与Raw元数据。
header/cursor/subject/body/bytes必须repr=False且默认序列化排除，不进入模型或日志；不新增业务事件。
