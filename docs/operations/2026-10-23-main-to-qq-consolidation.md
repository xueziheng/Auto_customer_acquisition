# 主环境数据迁入 QQ 升级环境

2026-10-23 按用户明确指令执行：QQ 原测试记录不进入新运行库；采用原主环境的企业数据、资料、账号与配置；旧主环境只留冷备；保留独立开发源码。工作在服务器完成，未使用 T7。

## 最终布局

- 公网入口：`https://trade.jslingte.com`；平台管理员入口 `/platform`。
- 运行代码：`/srv/tradeos/acceptance/qq-20261019/src`。
- 运行数据与配置：`/srv/tradeos/acceptance/qq-20261019/main-data-20261023`，profile 位于其 `profile` 子目录。
- 唯一应用服务：`tradeos-qq-acceptance-20261019.service`，使用 QQ 升级代码和原主环境数据；服务名沿用历史名称。
- 开发源码：`/srv/tradeos/development/TradeOS`，文件内容摘要与操作前一致。
- 原主环境及原 QQ 冷备：`/srv/tradeos/backups/pre-qq-consolidation-20261023`；恢复步骤见该目录 `README.md`。
- 受限执行记录：`/srv/tradeos/maintenance/consolidate-to-qq-20261023`。

原主服务及旧邮件验收 worker 已停止并屏蔽启动；原主 `/srv/tradeos/current` 别名撤除。旧运行目录归档到备份，精确归属的旧 PostgreSQL/对象存储容器和卷已退役。保留共享浏览器辅助组件；它们不是另一套 TradeOS 业务环境。旧 QQ 测试数据仅在冷备中，不在新运行库。

## 数据和配置保留

先冻结应用和存储，用现有 PilotProfile 冷备流程分别保存 main/QQ 数据库、对象、源码、前端构建、私有文件及 systemd/nginx 配置，两份归档均通过校验。

主冷备实际恢复到全新 owner、容器和卷。先用原代码恢复 schema0078，再由 QQ 代码迁移至0102。对156张原表按原字段逐表比较，除 schema 版本和撤销旧认证会话外没有差异；对象清单（键、大小、ETag）及知识库/Obsidian 文件树一致。

保留的数据包括6条账号记录、6份企业资料、7,618条邮箱记录、2个客户账户、5条业务消息、11条工作流记录、4个模型配置版本。数字是迁移时的原有记录，不是新增获客成果，也不表示所有账号都已启用。

保留33个已选获客源、23条来源备注、原 Hunter/SAM.gov/Apollo/Snov 配置；将原主已有 Tavily 凭证保留为可选来源。管理员模型/API绑定、企业资料、邮箱连接和原账号密码散列保留。部署路径指向新目录；Tavily 未恢复为默认搜索后端，未开启付费搜索。

## 迁移中修复

1. 原数据库角色缺少两张新增回复表的权限：只对原企业和平台两个角色补齐 `reply_classification_resumptions`、`reply_classification_retries` 的 SELECT/INSERT/UPDATE/DELETE。两种真实数据库身份随后通过完整租户隔离检查；RLS、角色绑定、密码和管理员权限没有放宽。
2. Hunter 已就绪配置的启动兼容：旧 runtime 已记录 READY 时，新客户准备 runtime 重复提交首次组合事件会抛 InvalidStateTransition。现在在 singleton 锁内重新核对当前租户、精确配置和 READY；尚未组合的配置仍走原组合事件。没有重新验证 Provider、改写验证记录或绕过调用前检查。

## 验证证据

- 迁移前配置、知识和邮件模拟回归：162 passed。
- 新增真实 PostgreSQL 回归在修复前复现相同 InvalidStateTransition：1 failed；修复后客户准备集成及启动兼容用例：27 passed，2个浏览器 E2E 未在该轮执行。
- `scripts/check_boundaries.py`：通过。
- 独立迁移复核及 Hunter 窄改动复核：没有未解决的重要问题。
- 三个服务健康检查通过；私网和公网分别验证管理员和企业登录、来源配置、模型绑定、6份资料原件读取、历史邮箱列表、退出登录。
- 旧资源退役后完整重启、公网复查均通过，邮箱和发送账本与迁移前一致；结果见维护目录 `restart-result.json`、`smoke-post-restart.json`、`final-verification.json`。

## 验证范围

自动发信、邮件回复监听和历史邮件测试服务保持关闭。本次没有发送真实邮件或同步远程邮箱；集成测试使用独立数据库与模拟邮件端口。33是保留的已选来源数量，不表示33个来源全部完成真实取数。真实获客、真实模型调用和真实邮箱收发不能由本次迁移验收代替。

备份保存在本服务器，未上传 GitHub。Git 仅保存源码和不含秘密的记录。原定时备份 timer 在任务前已停用，本次未新建周期任务。
