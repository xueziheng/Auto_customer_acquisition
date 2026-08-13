# tool_gateway/ —— 工具网关

## 职责

**所有对外部世界的动作的唯一出口**（硬边界 1）。Agent 和工作流提出工具调用，本模块跑完检查管线后才执行 handler。模型永远拿不到凭证——凭证只在 handler 内部，运行期从密钥服务取。

设计文档：`docs/architecture/04-tool-gateway.md`。

## 结构

```text
tool_gateway/
├── manifest.py        工具 manifest schema + 注册表
├── errors.py          固定安全错误、交付确定性与调用状态
├── repository.py      tenant-scoped ledger / UoW Protocol
├── fingerprint.py     HMAC 请求指纹（只持久化摘要）
├── pipeline.py        检查、claim、执行与恢复编排
├── checks/            每个 stage 一个文件
│   ├── tenant.py        租户一致性
│   ├── permission.py    RBAC + ABAC
│   ├── playbook.py      公司规则（排除品类/国家）
│   ├── country_policy.py 国家政策包
│   ├── suppression.py   抑制名单
│   ├── approval.py      审批状态
│   ├── idempotency.py   幂等
│   └── rate_limit.py    频率与配额
└── handlers/          工具执行器注册处（实现调 connectors/）
    └── email_send.py  Gmail 单封发送参数组装与恢复搜索
```

## 加新工具 = manifest + handler，不改管线

这是四个插件点之一。任何「加个工具要改 pipeline.py」的做法都是设计违规。

## Stage 顺序不可随意调换

便宜且否决率高的在前，贵的在后；幂等必须在执行前、记账后：

通用 manifest 的全序是：

```text
tenant → permission → playbook → country_policy → suppression
→ approval → idempotency → rate_limit
```

Phase 1 的 `email.send` 只启用六个与发送相关的 stage，精确顺序是：

```text
tenant → permission → suppression → approval → idempotency → rate_limit
→ 提交 EXECUTING 证据 → Gmail → Outreach 完成 Attempt → 完成 canonical tool_call
```

顺序错了会出现「重复扣费但没发送」或更糟的「重复发送」。

## 拒绝必须结构化

任一 stage 拒绝即终止，返回**结构化原因**（哪个 stage、什么规则、能否补救）。Agent 需要知道「为什么不行」才能换办法，而不是重试同一个调用。裸异常会被当成临时故障重试——那正是最不该发生的。

## 依赖白名单

```text
允许   shared.*、domains 的 service.py 显式接口（判权与查名单）、connectors.*
禁止   domains 的 models.py / repository.py
禁止   在本模块写业务规则——「该不该发」在域里判断，这里只查「能不能发」
```

## 持久 ledger 与审计

迁移 `0010` 提供 tenant-scoped `tool_calls` 与 append-only
`tool_call_events`。ledger 只允许安全 ID、固定分类、时间、耗时、成本说明、
provider reference 与 HMAC 请求指纹；**不得持久化邮箱地址、主题、正文、退订链接、
OAuth token、完整请求或异常文本**。请求 payload/preflight 只活在单次进程内。

每次调用（含拒绝与重复）都追加结构化 event。进入 Connector 前必须先提交
`EXECUTING` 和对应事件；这笔写入失败时不得调用 Gmail。完成 Attempt 或 canonical
ledger 失败时也不得伪造成功。**审计写入失败必须阻断动作本身**——审计不完整时
继续发客户邮件是合规裸奔。

HMAC 指纹 key 由运行时密钥解析器提供，只记录 key version。key 轮换时，旧版本在
ledger 保留期内必须仍可核验；否则相同幂等键只能固定报冲突，不能猜测或重算成新请求。

## Gmail 不确定结果恢复

`CLAIMED` 的 lease 过期且尚未提交 `EXECUTING` 时，可以重新跑**当前事实**与六个
stage。只要已提交 `EXECUTING`，或 Connector 返回“可能已经写入”，canonical 调用
就进入 `failed_transient / reconciliation_required`：后续只允许按确定性
`Message-ID` 与 `X-TradeOS-Idempotency-V1` 搜索，禁止再次调用 send。

**一次 Gmail 搜索未命中不等于邮件确定未发送；只要 Connector 可能已开始，系统不得自动重发。**

搜索命中时，先按原 provider reference 完成 Outreach Attempt，再完成 canonical
ledger；未命中继续保持人工对账；provider reference 不一致固定冲突，绝不覆盖原值。
只有 Connector 明确给出 `definitely_not_sent` 的临时失败，才可在 lease 到期后重新跑
当前事实；此时新增抑制、回复、身份或额度变化都可以停止重试。

## Phase 1 范围

manifest 注册表、固定 stage 编排、Postgres canonical ledger、append-only event、
`email.send` 单封 Gmail handler、租约恢复与人工对账边界。成本钱包仍是 Phase 3
挂载点；不在本阶段实现自动对账扫描器、对账 UI、回复 worker 或自动重发。
