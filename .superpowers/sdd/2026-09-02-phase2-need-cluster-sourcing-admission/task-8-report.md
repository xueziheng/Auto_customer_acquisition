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

- `feat: admit sourcing workflows by cluster priority`（hash 在提交后补录）。
