# NeedCluster Sourcing Admission 历史回填运维说明

本命令只为历史 `Sourcing Case V2` 补建缺失的 `Sourcing Admission`。它不会启动
Workflow，不会搜索网页、联系供应商、发现联系人、发信、采购或生成 Quote。回填后是否
执行，仍由当前生效且经老板确认的 `sourcing_admission` Directive，或授权员工的单条人工
准入控制。

## 适用范围

命令只会把同时满足以下条件的案例列为可回填：

- 属于本次显式绑定的 tenant；
- `workflow_version = 2`；
- Case 状态仍是 `opened`；
- 不存在任何同 tenant、同 Case 的 `sourcing_case` Workflow Run；
- 不存在同 tenant、同 Case 的 Sourcing Admission；
- `opened_at` 是不晚于本次检查时刻的严格 UTC 时间；
- 冻结 Need snapshot 的结构、Need 绑定和三方 hash 可核验，完整度仍达到 3；
- 生产 Demand priority reader 能返回当前、结构合法的 Need Cluster 排序事实。

已启动、已终结、已有 Run 或已有 Admission 的案例只报告固定跳过原因，绝不回收、重排
或重放。不同 Need 仍保持各自 Case、数量、单位、国家、规格及 Provenance，不做簇级合并。

## 执行前

1. **先停止所有旧 scheduler / API worker 领取新任务**，等待已领取的准入/start 调用结束；核对
   没有未解释的 `STARTING` 租约或未绑定 Run。旧进程不使用新版 canonical subject-lock 协议，
   因此不能让旧 worker 与 migration 或回填重叠运行。
2. 在旧任务已经排空、领取仍保持停止时执行 Alembic 到当前合法单 head，并核验
   `alembic_version = 0056`。0055 为 `sourcing_case` V2 Run 增加数据库级准入 guard 和同 subject
   唯一索引：首次创建要求 Admission 为 `STARTING`，idempotency key 精确绑定该 Admission 的
   tenant 与 Need；`WAITING`、`BLOCKED`、缺失 Admission、错误 key 或第二个同 subject Run 均拒绝。
   0056 将首次人工 actor/request 与短租约 claim token 分离并在数据库层禁止换 actor/key；命令不自动
   执行 migration，不得在 0055 或更早 schema 上运行 apply。
3. 部署并重启包含 canonical subject-lock 协议的同一新版本，确认所有实例的构建版本一致、旧版本
   进程为零后才能运行回填。数据库 guard 是跨入口的数据安全兜底，但**排空旧 worker、迁移、
   版本核验都是上线前提**，不能用普通暂停建议替代。
4. 回填写入口与新版 Workflow Run 创建共用同 tenant、同 workflow type、同 Case 的事务锁，
   并在锁内重新核验 Run 与 Admission。变更窗口内继续暂停 scheduler 与人工准入，便于把
   dry-run、apply 和复核作为一个受控批次；恢复前必须完成下方检查。
5. 由运维环境加载 `DATABASE_URL`。默认 dry-run 可从 `TRADEOS_TENANT_ID` 读取租户；
   `--apply` 不接受该隐式租户，必须在命令行再次写出精确 tenant ID。
6. 不在聊天、终端历史、工单或报告中粘贴数据库密码、Token、密钥值、Need snapshot 或
   Provenance 原文。命令输出不会包含这些内容。

## 先运行 dry-run

默认模式只读、零业务写入：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  PYTHONPATH="$PWD" python3 scripts/backfill_sourcing_admissions.py
```

也可显式指定 dry-run 与租户：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  PYTHONPATH="$PWD" python3 scripts/backfill_sourcing_admissions.py \
  --dry-run --tenant-id "tenant_REPLACE"
```

标准输出只有一行 JSON。`counts` 是本次安全分类计数；每个 `results` 元素只含
`case_id`、`status`、`reason`。`would_create / eligible` 表示按当前事实可以回填，
不表示已获准启动 Workflow，也不表示有供应、报价或成交机会。

常见固定跳过原因：

| reason | 含义 |
|---|---|
| `already_admitted` | 已有 Admission，幂等保留 |
| `workflow_run_exists` | 已有 Workflow Run，历史执行不回填 |
| `case_already_started` | Case 已离开 `opened` 并仍在处理中 |
| `case_terminal` | Case 已终结 |
| `tenant_mismatch` | 底层投影与显式 tenant 不一致，失败关闭 |
| `invalid_opened_at` | `opened_at` 不是可核验 UTC 时间 |
| `malformed_need_snapshot` | 冻结快照结构、绑定、完整度或 hash 不一致 |
| `priority_facts_invalid` | Demand priority facts 当前不能安全构造 |

Demand priority reader 的存储结果若未知，不会被伪装成 `priority_facts_invalid`；命令固定输出
`priority_facts_read_unknown`、停止后续处理并以退出码 3 结束。

dry-run 结果是某一时点的读取，不是锁定授权。修复坏数据时必须回到拥有该事实的域及其
正常写入口，不得手改 snapshot、Provenance、Admission 或 Run 表来迎合回填。

## 显式执行

核对 dry-run、备份和变更窗口后，使用同一个精确 tenant：

```bash
PATH=/Users/xueziheng/miniconda3/envs/tradeos-py312/bin:$PATH \
  PYTHONPATH="$PWD" python3 scripts/backfill_sourcing_admissions.py \
  --apply --tenant-id "tenant_REPLACE"
```

`applied / admission_ensured` 表示生产 Sourcing service 已幂等确保 Admission 及其首个 immutable
priority snapshot 存在。命令使用 Case 的原始 `opened_at` 作为 `ready_at`，不以执行时间改写
历史等待顺序。若正常 Run 在互斥边界上先完成创建，回填固定报告
`workflow_run_exists` 且不写 Admission；若 Admission 已存在则报告 `already_admitted`。Case 或
Need 在此期间发生的其它状态变化仍由生产领域服务复核；库存阶段验证过的 Case snapshot hash
也会传入领域命令，并在 Case 行锁内再次核对正文、内嵌 hash、持久化 hash 和 canonical hash。
拒绝时固定报告
`eligibility_changed`，不会绕过域门禁。

0055 的数据库 guard 与锁内校验分工明确：事务锁决定同一 Case 的先后顺序，guard 在 Run
INSERT 时再次核验持久 Admission。若回填先提交 `WAITING`，已经排队等待锁的 start 也会被拒绝，
不会形成 `WAITING + RUNNING`。只有领域服务 claim 后的 `STARTING`，配合
`sourcing-case:v2:{tenant}:{need_id}` canonical key，才可首次创建 Run；同 subject 最多一条。
若 tenant + idempotency key 已有 Run，则只有 workflow type/version/subject 全部精确一致才允许
幂等返回，包含 STARTING 未绑定恢复、ADMITTED 后的精确回放，以及 0055 前已启动且按
兼容规则不补 Admission 的历史 V2 Run。这条历史兼容只复用已有行，不新建 Run 或
Admission；如果该 Case 已有 Admission，仍必须核验 canonical key 及 STARTING/ADMITTED 的
Run 绑定。任何绑定不一致都固定拒绝且不披露 key、context 或 subject 内容。

执行成功后：

1. 复核 JSON 计数和每个 Case 的准入详情；
2. 确认当前 active Directive 是否包含老板确认的自动准入开关与 `batch_limit`；没有配置、关闭
   或状态未知时都必须保持零 Workflow start；
3. 恢复 scheduler；观察第一轮准入数量、排序解释和 canonical Run 绑定；
4. 需要人工启动时仍走原人工准入 API 与幂等请求 ID，禁止直接更新数据库状态。

人工请求结果不确定时，先读取同 tenant 的 Admission 与 canonical Run，再由同一 actor 以原始
`Idempotency-Key` 恢复。租约释放后该身份仍保留；scheduler 可使用新的内部 claim token 恢复同一
canonical Run，但不会改写原人工身份。0056 升级前已经存在的 actor-only 行没有可验证原键，系统不
猜造或回填 request ID：人工路径失败关闭，scheduler 仍可按 canonical key 收敛。

## 停止与未知结果恢复

- 参数或环境无效：输出 `status=not_run`、`reason=configuration_or_input_invalid`，退出码 2；
  没有进入回填执行。
- 数据库、提交或组合结果不确定：输出 `status=unknown` 或单项
  `reason=priority_facts_read_unknown / admission_write_unknown`，退出码 3。命令会停止后续处理，
  不能据此判断此前一项未提交。
- 遇到 unknown 时不要自动重试、删除 Admission、清快照或更换 Case/Need。保持 scheduler 停止，
  先按同 tenant、同 Case 核对 `sourcing_admissions`、current snapshot 和 Workflow Run；确认事实后
  再以同一命令重跑。`enqueue_admission` 的 canonical Case/Need 约束使合法重跑保持幂等。
- 本工具没有业务回滚命令。Admission 和 immutable priority snapshot 是审计证据，不允许通过
  删除记录“撤销”。若录入事实损坏，应修复来源事实并走既有 refresh/block 流程。
- 不得在回填窗口中 downgrade 0056/0055。0056 在存在未终结的人工 request identity 时拒绝降级，
  禁止为迎合 migration 清除 actor/key；0055 的 downgrade 是确定性的 schema 往返：它不检查、
  删除或改写 Admission、snapshot、Run，而只删除准入 trigger 和 V2 subject 唯一索引。因此
  Alembic 返回成功**不等于业务回退安全**，已有业务行也不应被删除成零来迎合 migration。
- 确需回退时，必须先停止 scheduler、人工准入、API 和所有可写 `workflow_runs` 的旧/新进程，
  排空在途事务，并通过正常恢复入口处置所有未解释的 `STARTING` 未绑定状态。变更单必须记录
  Admission 各状态计数、Sourcing V2 Run 总数，以及
  `(tenant_id, workflow_type, subject_ref)` 重复 V2 Run 数为零；这些是前后核对值，不是清理目标。
- 只有在回退版本明确关闭 Sourcing V2 自动/人工 start，且所有人工恢复身份均已通过正常终态收敛的情况下，
  才能先 `downgrade 0055` 再按需执行 `downgrade 0054`。
  完成后核验 trigger 和 `uq_workflow_runs_sourcing_v2_subject` 均已移除、上述业务计数完全不变；
  其它 Workflow 即使恢复，Sourcing V2 仍须保持停用。缺少 guard/index 时恢复旧 V2 写入口会重新
  开放未准入 Run 和同 Case 多 Run 的窗口。
- 重新升级到当前 head 前再次停止所有写入口并核验重复数为零；不得依赖 migration 替运维判断或修复
  回退窗口产生的数据冲突。备份、审批、兼容版本和恢复方案任一未知时都不执行 downgrade。

受控测试通过只证明命令与核心服务接线正确，不代表生产已执行回填、启用真实 Sourcing V2，
或授权任何外部来源调用。
