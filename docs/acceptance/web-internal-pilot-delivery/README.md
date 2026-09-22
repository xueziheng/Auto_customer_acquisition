# 持久化 Web 内测交付记录

本轮已完成五项任务并通过独立最终审查，交付范围是单租户、本机 loopback 的持久化 Web 内测。账号与当前权限、Cookie/CSRF、完整停启、冷备份恢复均有实际测试证据；真实 Provider、共享部署和桌面端不在本轮交付范围。

- [操作说明](../../operations/web-internal-pilot.md)：首次政策配置、TTY 创建账号、启动、停止和恢复。
- [验收结果](../2026-09-07-web-internal-pilot.md)：精确测试版本和证据边界。
- [最终审查](final-review.md)：Approved；零必修问题，保留两个非阻断测试改进项。
- [全部决策与代价](rulings.md)：包括错误判断的撤回、更正和最终保留项。
- [完整执行记录](progress.md)：五项任务、修复轮次、实际命令归属和审查门禁。
- [归档清单](manifest.json)：每份原记录的映射、SHA-256，以及可从 Git 重建的审查差异范围。

## 任务记录

| 任务 | 实现报告 | 原审 | 修复复审 |
| --- | --- | --- | --- |
| 1 账号、密码、会话与限流 | [报告](task-1-report.md) | [审查](task-1-review.md) | [复审](task-1-fix-1-review.md) |
| 2 API 认证与可信账号 CLI | [报告](task-2-report.md) | [审查](task-2-review.md) | 无需修复轮次 |
| 3 持久化 profile 与冷恢复 | [报告](task-3-report.md) | [审查](task-3-review.md) | [复审](task-3-fix-1-review.md) |
| 4 Web 登录与三进程运行 | [报告](task-4-report.md) | [审查](task-4-review.md) | [复审](task-4-fix-1-review.md) |
| 5 完整浏览器与生命周期验收 | [报告](task-5-report.md) | [审查](task-5-review.md) | [复审](task-5-fix-1-review.md) |

## 当前版本与已知限制

审查 HEAD 为 `6bb5f51d81df5e7f6d83f514bed5dac175efcca0`；最终生产源码为 `ecfb4d2d959ade7ffa143b7b9ad1b8e29cde4242`；增强完整 E2E 在 `63150da376b07414088f5c0f90bcb7ea5f22e075` 通过（1 passed，34.15 秒）。后续归档只新增文档，不表示重新运行了一次全仓测试。

保留两个测试改进项：恶意 tar 测试需要合法根基线以区分拒绝原因；console collector 的宽过滤可能漏掉部分并发告警。它们不影响已有核心行为直接断言，但不能宣称所有负路径分别获得强测试证明或所有运行时告警均被排除。经理排他锁的争用成本与四个未改页面的历史 87 条 lint warning 已披露，不宣称全库零告警。

分支 `codex/web-internal-pilot` 保留在 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`，未推送、合并或删除工作目录，历史 400 个未跟踪 output 文件保留。没有创建真实用户 profile 或默认公司政策/密码；首次使用需操作者完成操作说明中的显式配置。

原报告按原文归档，内含的 `.superpowers/sdd/2026-09-07-web-internal-pilot/` 路径及代码行号是审查时的位置；同名 Markdown 记录现在位于本目录。旧“pending”或被撤回证据须结合后续完成行、修复附录和最终审查阅读。仅该计划的临时目录会在归档核验并提交后移除，其他计划目录不变。
