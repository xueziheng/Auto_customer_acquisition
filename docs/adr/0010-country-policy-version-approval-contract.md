# ADR 0010：国家政策包版本与审批契约

- 状态：已接受
- 日期：2026-08-24

## 背景

国家政策会直接控制公开研究、联系人补全与冷 B2B 邮件等外部处理。部署 allowlist 或代码内
默认值不能说明依据、提交人、审批人和生效版本，也无法在租户边界内安全审计。把这些规则
塞进 Company Playbook 还会把法律合规政策与公司经营偏好耦合。

## 决策

1. 新增独立 `domains/compliance`，只依赖 `shared.*`；上层只使用其 `schemas.py` 与
   `service.py` 公共契约。
2. 政策内容写入不可变 `CountryPolicyVersion`，生效另写 append-only
   `CountryPolicyActivation`；批准、拒绝或应用失败均不回写候选内容。
3. 跨域激活只接受窄 `CountryPolicyApprovalFact`，逐项核对 approval、change-set、决定人和
   决定时间；合规域不导入审批域。
4. 新增共享强类型 ID `CountryPolicyVersionId` 与 `CountryPolicyActivationId`，值分别使用
   `cpp_` 与 `cpa_` 前缀。
5. 系统不提供任何国家的法律默认值、模板、别名或 ISO 推断。国家键仅执行 NFKC、去首尾
   空白、连续空白折叠和 casefold，随后精确匹配。
6. 每个决策字段保存独立、人工确认的 Provenance。客户端只提交安全来源类型与引用；
   `extracted_by/at`、`confirmed_by/at` 由可信 actor 和服务器 UTC 时间生成。
7. 首次配置和后续修改都需要不同于提交人的独立审批人；无 bootstrap、force、直接激活或
   apply-now 旁路。

## 结果

系统可以复原每个生效判断对应的精确候选、来源、批准事实与应用顺序，并在未知国家或缺失
配置时保守拒绝。代价是每个国家需要显式录入九项字段来源并完成独立审批，读取当前政策也
需要组合版本与 activation；该成本换取可审计、可重放且不靠隐含法律结论的合规边界。
