# QQ 私有回信隔离验收记录 — 2026-10-15

## 结论与范围

隔离实现和本轮受控验收完成；未部署，不代表真实QQ收发、真实模型准确率或全仓验收通过。
唯一测试pair为1249583590@qq.com → 986647123@qq.com，qikai保持sales；
分类使用独立配置的当前真实boss权限，不通过给销售提权解决。

- 开发目录：/home/admin/tradeos-qq-reply-20261015
- 线上来源：/srv/tradeos/releases/20261014-manual-aa08ea2
- 来源实际提交：c2f67efdc8e9a553704fc2fcc7fb82461fe9e794
- 隔离Git基线：7d76a14794835648f86441647c0ef5b5f0027a0a
- 变更保留在隔离工作目录，未提交、推送或发布。
- 规范：[ADR0098](../adr/0098-qq-private-reply.md)
- 后续发布及回退：[部署计划](qq-private-reply-deployment.md)，仅文档，未执行。

## 实际实现

1. connectors/qq_mail/inbound.py、inbound_transport.py及infra/db/qq_inbound.py：
   固定QQ只读IMAP、精确项目回复头及当前数据库归属授权，正文前过滤和正文后重验；
   UID/UIDVALIDITY游标、重放幂等、取消连接清理。无关邮件正文不读取、不归档、不送模型。
2. apps/composition_support/email_inbound.py及原email_inbound Gateway：
   注入provider游标适配，复用原原件存储、整页事务、Outbox和Attempt账本。
3. standalone/settings、scheduler/bootstrap/runtime/reply_composition：
   收回复与自动发送分离；reply-only不注册发送handler/流程/driver，不处理旧排队发送。
   私有测试正向回复进入原负责人工作项，不创建Need/Hypothesis/Opportunity/Handoff。
4. private_reply_test契约、应用及存储、0077迁移：
   明确同意、本人绑定、精确pair、当前owner、单步获批Campaign、单一sender、manual；
   联系人保持unverified，不伪造公开证据；普通未验证联系人不放行。
   新表强制RLS、固定租户角色双策略、现有角色授权；已使用用途拒绝降级。
5. conversations服务/仓储、Inbox路由及SmartInbox：
   当前权限先过滤再LIMIT；当前owner锁和CAS状态转换；原页面处理与通知深链；
   身份切换、撤权和迟到响应保护。私有准备入口位于原邮件撰写页。
6. notification_projection、原job/worker、in_app_notifications：
   正向回复只通知当前boss、LOW站内、持久去重；投递时重新校验老板角色。
   ReplyReceived从同事务持久消息补齐conversation_id；已纠正的旧正向事件不再新建通知。

## 最终测试结果

所有日志在本目录根的.test-results/，批次有重叠，禁止相加。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| 统一相关后端回归，独立新库 | 640 passed，0 failed，3 skipped，8 deselected，114.25s，EXIT 0 | check-1792039603357623093.txt |
| 前端SmartInbox与私有准备 | 14 passed | web-1792038315275662312.txt |
| API与平台API类型生成 | EXIT 0 | web-1792038293738015984.txt |
| npm run build（含vue-tsc --noEmit） | EXIT 0 | web-1792039072454857927.txt |
| 所有变更Python文件Ruff | EXIT 0 | check-1792039348375621411.txt |
| scripts/check_boundaries.py | EXIT 0 | check-1792039384621182456.txt |
| git diff --check | EXIT 0 | final-evidence.json |
| 后端mypy | 未运行，测试venv未安装；未安装新依赖 | final-evidence.json |

最终后端命令及精确文件列表完整保存在对应日志和final-regression-command.json。
最终库tradeos_reply_final_20261015从空库实际upgrade到0077；原fixture库保留。
专用容器tradeos-qq-reply-fixture-20261015为network=none，无真实邮箱、模型或公网调用。
前端仅在隔离目录构建，产物未发布；原有PlatformConsole重复导入产生非阻断构建警告。

完整三类fixture链包含原手动发送Attempt → 精确QQ回复 → 原入站Gateway →
真实数据库/Outbox/workflow → 原model.generate Gateway（仅外部模型响应为fixture） →
分类/原owner工作项/HTTP处理 → 原notification worker站内投递。
覆盖明确兴趣、愿意继续/WhatsApp联系、无当前需求，保留不同语义；
另覆盖未知发送不重试、同意撤销、owner转派、老板撤权、重复轮询/通知、
无关正文零读取及正式业务成果零新增。
0077验收包含新库upgrade→downgrade→upgrade、schema/ORM匹配、真实租户角色隔离和使用后拒绝downgrade。

## 未验收项与中间失败的处理

- 3项旧notification_dedup容器级用例在隔离进程中无法访问Docker，pytest明确skip。
- 8项旧conversations_correction用例依赖owned_infrastructure完整栈。
  精简断网进程未提供其要求的Node/基础设施入口，最初setup报dependency_missing；
  服务器本身有Node v24.21.0，不能表述为服务器缺Node。
  最终明确deselect这8项，未修改断言、未把它们计入通过数。
  原始错误记录check-1792039018341690777.txt（640 passed、3 skipped、8 errors）。
- 一次复跑复用已使用fixture库，与旧通知测试固定ID冲突：
  check-1792039440962523560.txt为16 failed、624 passed、3 skipped、8 deselected。
  没有修改通知逻辑或清空历史账本；改用新独占空库后，同一测试集合全部适用用例通过。
- 本轮发现并修复0077缺失RLS导致启动拒绝、分类事件遗漏conversation_id；
  schema比较改用实际方言类型编译，运行时测试正确注册旧队列测试handler。
- 未运行全仓测试、后端mypy、真实浏览器操作验收、QQ真实投递/收信或外部模型调用。
  fixture证明流程及权限，不能证明真实分类准确率或邮件送达。

## 线上与老板目录保护证据

live-preservation.json记录：
线上current仍指向上述版本，实际提交未变；baseline-manifest的2877个文件全部匹配，
missing=0、changed=0。tradeos为active/running，启动时间仍为2026-10-14 23:09:19 CST。
未执行生产写库、迁移、开关变更、部署、重启、真实收信或发信。

老板目录/srv/tradeos/development/TradeOS的HEAD仍为
22ece5a86d374795437407a67154ceba2973e1b9；现有dirty状态记录在证据JSON。
本任务未写该目录；缺少其原dirty文件逐文件哈希，因此不声称已做完整内容一致性证明。

## 发布与回退要求

另行部署前复核线上/老板分支漂移、审查补丁、完成可恢复备份并明确执行0077。
强制核对企业角色RLS，保持automatic_send_enabled=false；
启用真实QQ读取、真实手动发信和正文外部模型处理均须对应授权。
无用途数据才允许降级0077；有用途数据时不可删标记强行降级，
应使用兼容0077且关闭QQ功能的修复版本或一致备份恢复。
完整具体步骤见部署计划。
