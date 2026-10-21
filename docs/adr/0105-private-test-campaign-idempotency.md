# ADR 0105：按已批准单封 Campaign 区分私有测试

日期：2026-10-19。范围：用户明确要求在服务器继续测试的两个自有 QQ 邮箱。

## 已核实的问题

第一次 A 邮件已发送且单步 Enrollment 已 completed。用户授权修复后继续真实验收，
新建并批准的单封 B Campaign 却无法准备：旧入口使用仅含 account 的全局入组键，
原 Outreach 域正确拒绝将这个键绑定到另一个 Campaign。

## 决策

修订 ADR0098 的“一个私有对象永远只能一个单封流程”：新的已批准单步 Campaign
使用 account + campaign 的稳定键。没有随机键、失败自动换键或自动补发入口。
如果该 account/campaign 已有合法私有测试流程，必须沿用其原键，包括升级前的旧键。
多记录、未知键或非 manual 绑定拒绝。继续使用原 Outreach.enroll 和 email.send。

Campaign 必须重新经过原老板确认；收件人确认、当前归属、活跃 Enrollment、已有回复、
全局抑制、发件身份、单步/单 sender、发送额度等原门禁保持。未知发送不能通过新任务
绕过仍活跃的旧流程。同一新 Campaign 重复点击仍只有一个 Enrollment 和一个发送 Attempt。

这项修改不会允许对已回复账户自动开展下一轮。若原回复门禁拒绝，保留拒绝，
不得删除旧回复、篡改状态或换对象规避。私有测试仍不创建正式需求或商机。

## 验证

真实 PostgreSQL + 原应用 API + 原发送 Gateway 的合成测试覆盖：旧键恢复、新 Campaign
准备、同一任务重复调用不多发；实际邮箱和模型效果另外记录，不以合成结果代替。
