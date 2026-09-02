# Task 8 Report — Directive 门禁的 Sourcing Admission Driver

## 状态

完成。scheduler 现在只在 active Directive 的 `sourcing_admission` 段明确启用时，按该段的
`batch_limit` 对 durable Admission 做精确 claim，并以既有稳定业务键启动、绑定 Sourcing Case V2
Workflow。没有修改 Tool Gateway，没有实现 Task 9 HTTP/manual admission。

## RED / GREEN

- 初始 RED：新 driver 测试收集时因 `DirectiveSourcingAdmissionPolicyReader` 不存在而报错；排除该新
  文件后为 `6 failed, 81 passed`。失败分别证明 scheduler 尚无 runtime driver/锁内 phase 顺序与
  production composition，覆盖 missing/disabled/unknown policy 零 claim/start、Directive 严格字段、
  exact policy batch、repository 返回顺序、逐项失败隔离、稳定幂等键和 crash recovery。
- 最小实现后，driver unit 为 `16 passed`；brief 指定的四文件 focused 回归最终为
  `104 passed in 6.54s`。
- 真实 PostgreSQL composition 加强证明：首次 `WorkflowEngine.start` 成功、bind 注入暂态失败后，
  Admission 保持 `starting`；租约到期重扫使用同一幂等键，数据库只有一个 canonical Run，最终绑定
  成功。定向结果 `1 passed in 4.98s`。

## 实现边界

- `DirectiveSourcingAdmissionPolicyReader` 只把 active `DirectiveView` 的完整 sourcing admission 段
  投影为 policy；无 active/整段缺失返回 `None`，存储失败、部分字段、unknown mode、非 exact bool、
  非 exact int 或 `batch_limit` 不在 `1..50` 均转为固定、detached 暂态错误。
- production root 用显式 SYSTEM Employee actor 和固定 tenant-bound adapter 组合 Directives service；
  employee 读取不允许跨 tenant，重复 ID 去重。active Directive 仍由 Directives 域的老板确认与只增
  版本不变量产生。
- policy 缺失、disabled、unknown 返回三种固定 scan result，均不 release/claim/start。enabled 才先
  释放到期租约，再把 policy 自己的 batch limit 原样传给数据库 claim；没有复用全局 scheduler
  `batch_limit`，也没有应用层重新排序。
- 每条 claimed Admission 独立处理。已知 transient start 固定退回 waiting；已知 permanent 固定
  `case_state_mismatch` block；unknown start 不泄露异常、不主动 release，保持 starting 等 lease。
  start 之后 run ID/bind 结果不确定也保守等待 lease，不生成新键。
- `_safe_context` 原样原子迁移到 scheduler 私有可复用 helper；start 参数保持
  `tenant / sourcing_case / case_id / safe_context / sourcing-case:v2:{tenant}:{need}` 不变。
- scheduler 顺序固定为 `outbox_pre → campaign → quote_expiry → sourcing_admission → workflow →
  outbox_post`；admission 前立即重新确认 dedicated lock。锁丢失直接阻止 admission 及后续 phase；
  普通 admission 失败只做固定脱敏 phase 日志并继续 workflow。
- driver 日志仅含 tenant、固定 reason 和计数；scheduler phase 日志沿用固定 message、exception type
  与 cycle metadata，不记录原异常文本、claim token、Need/Case/Run ID 或 workflow context。
- 顺手把 `sourcing_events.py` 首行从立即 Run 语义修正为 durable Admission，完成 Task 7 唯一 minor。

## 最终验证

```text
pytest Task 8 brief focused：104 passed in 6.54s
真实 PostgreSQL start-before-bind recovery：1 passed in 4.98s
Ruff touched implementation/tests：All checks passed
mypy touched production boundaries：Success, 6 source files
Python 3.12 boundaries：7 项全部通过
Task 6-8 + scheduler/quote-expiry 必要扩大回归：384 passed in 10.90s
provider-readiness migration 隔离复跑：1 passed in 4.86s
git diff --check：exit 0（持续出现仓库既有 AppleDouble pack index warning）
```

## 疑虑 / 环境限制

- 全库 `pytest -q` 首轮在浏览器 E2E 阶段出现 `5 failed, 6 errors`；共同根因为本 worktree 缺少
  `apps/web/node_modules/vite/bin/vite.js`，Vite 子进程全部在测试服务 ready 前退出。中断前已有
  `361 passed`。base 同样不跟踪该依赖，且相关 E2E 文件相对 base 零 diff。
- 扩大 `pytest -m 'not e2e' -q` 仍收进一条未标 `e2e` 的浏览器用例；按 Task owner 指示在 8% 中断，
  当时为 `789 passed, 12 deselected, 2 failed`。第一项仍是上述 Vite 环境缺失；第二项是无关的
  provider-readiness migration round-trip，相关源码相对 base 零 diff，隔离复跑通过，判定为前序
  中断/扩大套件数据库状态干扰。Task 12 再负责最终全量验收。
- 共享 Git 对象库持续报告既有 `._pack-*.idx non-monotonic index` AppleDouble 警告；没有触碰或
  修复这些共享对象。

## Commit

- 初始实现：`9dffa46` — `feat: admit sourcing workflows by cluster priority`
- 第 1 轮审查修复：`fce07eb` — `fix: start sourcing from frozen case snapshot`。

## 第 1 轮审查修复：以 canonical Case frozen snapshot 启动

审查探针确认初始 driver 在 claim 后重新读取可变 current ValidatedNeed，会让 frozen Case snapshot A
启动成 Run context B；同时没有在 start 前重验 Case 的状态、workflow 版本与 Need 对齐。

审查 RED：

```text
canonical Case driver + public service 定向：11 failed, 16 passed
```

失败精确覆盖 frozen A/current B 被替换、terminal/discovering、V1、Case Need mismatch、snapshot Need
mismatch、缺 snapshot/hash 均错误 start，以及 known transient/unknown canonical read 未生效和 public
读取能力缺失。

最小修复：

- `SourcingService` 增加 `get_admission_case_snapshot`，复用现有 `SourcingCaseReadView`，先按既有
  `ADMISSION_COMPLETE + SYSTEM` 判权，再做 tenant-bound Case repository 查询；没有把通用
  `CASE_READ` 授给 SYSTEM，没有新增 DTO、repository 旁路或 claim/secret 字段。
- driver 完全移除 current `SourcingNeedReader` 依赖。每条 claim 在 start 前读取 canonical Case，严格
  校验 Case/Admission ID 与 Need 对齐、exact V2、OPENED、frozen snapshot 与 64 位小写十六进制 hash；
  `_safe_context` 只接收 Case frozen snapshot。
- 永久 Case/Need/version/frozen mismatch 零 start 并固定 `case_state_mismatch` block；known transient
  read 脱敏后 release waiting；unknown 保留 starting 等 lease。原 policy 三态、DB claim 顺序/batch、
  scheduler lock/order 与稳定幂等键恢复均未改变。
- 真实 PostgreSQL probe 在 durable enqueue 后把 current ValidatedNeed 改为 B，最终 Run context 仍为
  frozen A 的 category/hash；同时继续覆盖 start-before-bind 失败后 lease 恢复只产生一个 canonical Run。

审查 GREEN：

```text
新 driver/public service 定向：27 passed
真实 PostgreSQL frozen A/current B + canonical recovery：1 passed in 4.93s
Task 8 brief focused：114 passed in 6.15s
Task 6-8 + scheduler/quote-expiry/service/repository/permissions：436 passed in 10.35s
Ruff affected files：All checks passed
mypy affected production boundaries：Success, 4 source files
Python 3.12 boundaries：7 项全部通过
git diff --check：exit 0（仓库既有 AppleDouble warning 仍在）
```

## 第 2 轮审查修复：三方 frozen snapshot hash 完整性

审查只读探针进一步证明，专用 canonical Case read 虽已返回 frozen snapshot，却没有比较 Case 持久化
`need_snapshot_hash`、快照内嵌 `snapshot_hash` 与冻结正文 canonical 重算 hash；因此持久 hash 为 B、DTO
内嵌 hash 为 A 的历史漂移仍可能启动。

本轮继续严格 TDD。unit RED 中两个漂移分支均因 service 未抛永久错误而失败；修正真实 PG fixture 的
外键插入顺序后，两个 PG 探针均观察到旧代码实际 `admitted_count == 1`，证明会调用 Engine 并错误
admit，而不是测试搭建错误。

最小修复：

- 将 `PostgresSourcingNeedReader` 原有的 canonical JSON（排序 key、紧凑分隔符、保留 Unicode）和
  SHA-256 算法原样提取为 `canonical_sourcing_need_snapshot_hash`，放在拥有
  `SourcingNeedSnapshot` 契约的 sourcing schema 模块；快照创建与完整性读取共用唯一实现。
- `get_admission_case_snapshot` 保持原安全 DTO 和 `ADMISSION_COMPLETE + SYSTEM`、tenant-bound
  repository 路径不变；在领域 Case 上严格校验持久 hash 格式、内嵌 hash、canonical 重算 hash 三者
  完全一致。缺失、非法或漂移只抛固定 `寻源准入 Case 冻结快照完整性无效`，不修复历史数据，也不
  透传底层/Pydantic 异常文本。
- driver 无需新增分支：固定 `ValidationError` 沿既有 permanent pre-start 路径原子写入
  `case_state_mismatch`；known transient 仍 release waiting，unknown 仍保留 starting 等 lease。
- 真实 PG 参数化探针分别注入持久 hash 与内嵌 hash 不同，以及两者相同但 quantity 正文被篡改；两者
  均 zero start、fixed block。原 frozen A/current Need B、首次 bind 失败后同一 canonical Run 的完整
  production composition 测试仍通过。

审查 RED / GREEN 与最终门禁：

```text
unit RED（三方 hash）：2 failed, 4 passed
真实 PostgreSQL RED（三方 hash）：2 failed；旧代码两项均 admitted_count == 1
三方 hash unit + PG GREEN：8 passed in 4.19s
Task 8 brief focused：117 passed in 6.50s
service / repository / permissions：277 passed in 8.16s
相关 sourcing event：22 passed in 0.33s
Ruff affected files：All checks passed
mypy affected production boundaries：Success, 3 source files
Python 3.12 boundaries：7 项全部通过
git diff --check：exit 0（仓库既有 AppleDouble warning 仍在）
```
