# Task 1 Fix round 1 限定复审

## 双门禁结论

- **Spec compliance：✅ 通过。** 原 Important I1 已修复；原同版本 active 实际 startup 证据提示已补齐。
- **Task quality：Approved。** 修复范围内未发现新缺陷，新增计数 Critical 0 / Important 0 / Minor 0；原阻断剩余 0。
- 本结论绑定 `acd89d23ca289f92bdbe5a280cbdf70fb05384f0..b52f833244560e7e22d18b562519a2120d2f277a`，结合初轮 `task-1-review.md` 的未受影响证据，仅是 Task 1 修复复审，不是最终全分支 gate。

## 逐项裁定

### I1：ADDRESSED（已解决）

- `infra/db/repositories/opportunities.py:585` 仅把 Employee 的 `with_for_update()` 改为 `with_for_update(key_share=True)`；在 PostgreSQL、默认 `read=False` 下为 `FOR NO KEY UPDATE`。该模式与转移历史 FK 的 KEY SHARE 兼容，消除了初轮实测的“guard 持 Employee 等 OwnershipLock；transfer 持 OwnershipLock 等 Employee”的循环。租户条件、取得事实的顺序、当前事实判断以及其余业务行锁不变。
- `tests/integration/test_owner_handoff_reminders.py:589` 复现原竞争的明确先后条件：真实 `OwnershipRepositoryImpl.replace` 已取得归属行；实际 SQL 执行完成的事件屏障确认 guard 已取得旧员工锁；再允许真实历史 flush/commit。断言不仅排除 `40P01`，还要求两个操作结果准确为 `committed` / `suppressed`，并验证一条正确的转移历史、新账户 owner、仍 requested 的 handoff 和零站内行。异常只提取 SQLSTATE，任务与监听器有 finally 清理和有界 timeout。
- `tests/integration/test_owner_handoff_reminders.py:703` 验证较弱锁仍保持所需写互斥：真实 EmployeeRepository 停用 UPDATE 在 guard 持锁期间不能提交，站内先提交后停用才能完成；之后 append 被抑制，历史行保持一条。它与 NO KEY UPDATE 对普通 UPDATE 的互斥语义一致，并非只检查 SQL 字符串。
- 接受串行边界依赖的 Handoff 行锁和最终站内 commit 均未改动；初轮 `:183` 的真实接受竞争用例及其他原14例包含在本轮17例 PG通过报告内。修复未通过取消当前事实锁来规避死锁。
- `docs/adr/0069-owner-handoff-reminders.md:33` 撤销了初版忽略隐式 FK 的“没有逆向锁”结论，记录实际死锁、精确锁模式和适用局限；不存在将本次窄修复扩大为全库无死锁保证的表述。

### 同版本 active 实际 startup 证据：ADDRESSED（已解决）

- `tests/integration/test_owner_handoff_reminders.py:677` 经真实 API composition 的 workflow engine 先持久创建 v7201 active Run，然后进入真实 API lifespan 和 scheduler runtime，两者均成功进入上下文；同时断言 Run 仍为 running/v7201，站内未被额外写入。
- 此新增用例补齐初轮指出的 helper 与真实 startup 证据差异。初轮真实无 active 正向和三类 active 不兼容拒绝用例仍由本轮完整提醒文件覆盖；无需修改运行时逻辑来让新增正向用例通过。

## 验证证据与边界

- 已读取 task brief、实现报告 Fix round 1 和给定三个文件的修复 diff；按限定范围复核 I1、启动证据及修复引入的新问题，复用初轮根/就近 AGENTS 和相关既有路径检查结果，没有重新进行全任务或全库审查。
- 实现报告记录生产修复前的真实竞争测试 RED：`1 failed / 15 deselected`，失败于排除 `40P01` 的断言；修复后新增三例 GREEN：`3 passed / 14 deselected`。测试代码的屏障、真实数据库副作用和断言与该证据相符。
- 修复 HEAD 的实现者最终记录：owned PostgreSQL 提醒文件 `17 passed / 6.33s`（无 skip，原14例+新增3例）；flow 单测 `21 passed / 0.14s`；两个变更 Python 文件 ruff、一个源码模块 mypy、七组 boundaries 与 diff-check 通过。上述是复用的实现者执行证据，审查者未重复运行同代码套件或实验。
- 初轮审查专用 `reviewer_lock_probe.py` 的通过语义仍是“在旧 HEAD 检出40P01”；本轮未重跑、未改写该历史探针，也未把初轮复现计为新 HEAD 通过证据。本轮新回归要求无死锁且转移提交/旧通知抑制，语义明确相反。
- 未发现需要追加实验的未解并发疑点。未读凭证、DSN、私有 profile、历史原始输出，未操作 owned 资源；仅写本复审报告，未改源码/index/HEAD、未提交推送、未派子代理。
- 未运行全分支最终 gate、真实 Provider、浏览器或真实业务 profile 验收。主动退回、Agent 接续、金额分档关闭等仍不属于本 Task。
