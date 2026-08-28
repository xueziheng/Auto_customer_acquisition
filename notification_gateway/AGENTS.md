# notification_gateway/ —— 通知网关（浅）

## 职责

统一通知出口。业务侧只发「通知事件」，渠道适配器决定怎么送达。业务代码**不知道**通知最终走站内、邮件、macOS 还是企业微信——这是插件点 4。

## 通知内容标准

设计稿第二十四节：通知必须自带上下文，不能只是「有个高意向客户」。

```text
客户 / 国家 / 需求 / 数量 / 当前负责人 /
为什么重要 / 下一步建议 / 截止时间 / 任务链接
```

## 路由规则

按事件类型 + 接收人偏好路由。高优先级事件（接管请求、身份熔断、承诺逾期升级）多渠道并发；低优先级只进站内。**渠道失败重试不阻塞主流程**——通知没送到不能让熔断本身失败。

## 渠道适配器

```text
channels/
├── in_app.py      站内（Phase 1）
├── email.py       邮箱（Phase 1，走 TRANSACTIONAL 身份，不占冷发额度）
├── macos.py       macOS 系统通知（Phase 3，桌面端）
└── wecom.py       企业微信 / OpenClaw 插件（Phase 2）
```

新渠道 = 实现 `NotificationChannel` Protocol + 注册。

## 依赖白名单

```text
允许   shared.*、connectors.*
禁止   domains 内部、业务判断（「什么值得通知」由各域的事件决定）
```

## Phase 1 范围

事件模型、路由、站内与邮件两个渠道。企业微信与 macOS 留接口。

## 报价结果窄增量

quote_approval_result只接受QuoteApprovalResult固定metadata：primary为quote_id、secondary
为run_id，reason限approved/rejected/expired/obsolete、level为空；优先级必须LOW，深链只为
/costing-quotes/quotes/{quote_id}。沿原workflow幂等键与稳定命名空间指纹去重，不能携金额/原文。
仅此kind可经quote_results.valid_quote_result_recipient保留ASCII safe-label<=32的持久短员工，
拒现secret marker；notifier、模板与InApp共用此helper。旧kind canonical员工门和邮箱目录不变。
worker需真实job→claim→模板→原router→in_app才算投递；API原structured_log不等于站内投递。
