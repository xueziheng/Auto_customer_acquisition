# 未分类回信的一次人工恢复

Spec: ../../../../review/未分类回信安全重试方案-20261019.md（用户已批准）。

全局约束：同一规范 Run；仅确定未派发的 rejected sequence=0；原失败、试验和额度保留；真实 UI 操作；生产不变。

## Task 1 — 持久授权与恢复事务
- 新增 workflows/reply_qualification/retry.py 的 DTO、纯前置条件及应用门面。
- 新增 infra/db/reply_retry.py、reply_retry_tables.py 与迁移0081。唯一 tenant/message 和 tenant/key，保存原 invocation、失败 step、操作者与 owner 快照、原配置、有效期及预算。
- 使用原当前 Inbox 权限。锁 step→run，当前归属与员工重核，保留失败 step，追加 classify step 与恢复记录，原子提交。
- RED：真实 PG 测试正常、重复/并发、跨租户/归属变化、已派发/未知、已有分类、配置变化；Expected: 缺少恢复模块。
- GREEN：同组测试通过，迁移 upgrade/downgrade/upgrade 通过。

## Task 2 — 受控模型与分类提交
- 只在持久授权存在时选用 sequence=1；原 sequence=0 绑定不放宽。
- 显式恢复预算独立于已停止的 QQ 试验，最大1次分类/4轮/32768输入字节/512输出token/60秒，并与全局限制取小值。
- 当前操作者、owner、原件绑定、模型版本在原件读取/派发/返回/提交前重验；未知或过期不再调用。
- RED→GREEN：授权漂移、缺预算、并发、未知、超时、无退款、原失败保持；原模型和QQ相关测试继续通过。

## Task 3 — API 与日常 UI
- GET 恢复状态、POST 空命令+原Idempotency-Key；服务判权；响应仅安全状态。
- 消息详情组件“重新识别”，身份代际与403清除；未知保留原key，只读刷新，不自动重发。
- RED→GREEN：API权限/空命令，UI点击一次、刷新、未知/换身份；生成OpenAPI类型、typecheck/build。

## Task 4 — 审核、部署与真实闭环
- 原边界脚本、相关单元/PG/迁移/UI检查；新上下文 reviewer 全量审核并修复重要问题。
- 新清单及rollback，仅测试实例升级；健康检查与生产路径确认。
- 从UI对C重试一次，读取真实模型分类、负责人工作项和老板通知，再UI处理工作项。
- 结论区分人工恢复与原自动失败；E购买场景/长期收信仍分别列出。

Review Focus: 权限锁顺序与并发、原步骤历史、未派发证据、配置漂移、未知与崩溃、已停止试验的严格显式替代、全局和逐轮预算、UI身份切换、私有测试不晋升正式业务成果。
