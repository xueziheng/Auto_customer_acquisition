# ADR 0013：资料请求使用明确的接管触发语义

- 状态：已接受
- 日期：2026-08-25

## 背景

回复类别 `requests_materials` 的确定性动作包含人工接管。既有 Opportunities 接管触发词表
只有报价、样品、规格等值，Outreach Campaign 的 `handoff_triggers` 公共边界也没有资料
请求。生产组合因而只能把明确的资料请求降级成 `agent_low_confidence`，丢失客户真实意图，
并让 Campaign 配置无法精确表达此类接管。

该值同时出现在 Opportunities 公共 handoff 契约与 Outreach Campaign 公共配置契约，属于
跨域公共词表扩展，不能只在 app 层发明字符串。

## 决策

在 Opportunities `HandoffTrigger` 与 Outreach Campaign 允许触发词表中同时增加
`materials_requested`。`requests_materials` 回复无论是否提取出需求字段，都使用该触发值
请求完整人工接管；缺少唯一 opportunity 映射时继续 fail-closed，不改用其他触发词猜测。

## 理由

新增明确值能保留客户表达的事实，并让接管队列、Campaign 配置与统计使用同一语义。
资料请求本身已足以要求人工判断「发什么」，不应依赖数量或规格提取成功；把它归为低置信
会混淆模型不确定性与客户明确动作。

## 放弃的选项

- **继续使用 `agent_low_confidence`。** 无需改契约，但语义错误且无法区分真实低置信案例。
- **复用 `specification_file_received`。** 两者都涉及材料，但一个是客户索取资料，一个是
  客户提供规格，方向相反。
- **只在 Opportunities 增加值。** Outreach Campaign 无法配置同名触发，跨域词表继续
  不一致。

## 后果

这是加法兼容变更，不修改历史记录，也不需要数据库数据迁移；现有 Campaign JSON 和 handoff
行继续可读。发布时必须先部署能读取新值的 Opportunities/Outreach consumer，再启用产生
该值的 reply composer，避免旧严格 reader 拒绝新枚举。

Campaign revision 只有显式包含该值时才改变其配置；本变更不自动修改已批准 Campaign，
也不绕过接管 owner、SLA、证据与幂等规则。

## 何时重新审视

当接管触发词表改为共享版本化契约、或资料请求被拆分为目录、认证文件、技术数据表等需要
不同 SLA 的子类时重新审视；在此之前保持单一 `materials_requested`，不在 app 层扩展别名。
