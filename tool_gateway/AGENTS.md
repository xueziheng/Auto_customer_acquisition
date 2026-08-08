# tool_gateway/ —— 工具网关

## 职责

**所有对外部世界的动作的唯一出口**（硬边界 1）。Agent 和工作流提出工具调用，本模块跑完检查管线后才执行 handler。模型永远拿不到凭证——凭证只在 handler 内部，运行期从密钥服务取。

设计文档：`docs/architecture/04-tool-gateway.md`。

## 结构

```text
tool_gateway/
├── manifest.py        工具 manifest schema + 注册表
├── pipeline.py        检查管线（stage 的编排，顺序固定）
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
```

## 加新工具 = manifest + handler，不改管线

这是四个插件点之一。任何「加个工具要改 pipeline.py」的做法都是设计违规。

## Stage 顺序不可随意调换

便宜且否决率高的在前，贵的在后；幂等必须在执行前、记账后：

```text
tenant → permission → playbook → country_policy → suppression
→ approval → idempotency → rate_limit → 执行 → 记录 tool_call
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

## 审计

每次调用（含被拒的）记 `tool_call`：谁、何时、哪个 Run、什么工具、什么参数、结果、耗时、成本。凭证类字段脱敏。**审计写入失败必须阻断动作本身**——审计不完整时继续发客户邮件是合规裸奔。

## Phase 1 范围

管线、manifest 注册表、八个 stage、审计。成本 stage 是占位（Phase 3 接积分钱包，位置留好）。
