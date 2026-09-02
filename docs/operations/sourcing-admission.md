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

1. **先执行 Alembic 0055 migration**。它为 `sourcing_case` V2 Run 增加数据库级准入 guard：
   新建 Run 时 Admission 必须处于 `STARTING`；`WAITING`、`BLOCKED`、缺失 Admission 均拒绝。
   命令不自动执行 migration，不得在 0054 或更早 schema 上运行 apply。
2. 部署包含同一 canonical subject-lock 协议的新应用版本，停止领取新任务，排空并重启所有旧
   scheduler / API worker；确认没有旧版本进程后再运行回填。数据库 guard 能拦住旧进程产生
   未准入的新 Run，但旧进程不使用新版锁 identity，因此**迁移与排空顺序仍是上线前提**，不能
   用“建议暂停”代替版本切换。
3. 回填写入口与新版 Workflow Run 创建共用同 tenant、同 workflow type、同 Case 的事务锁，
   并在锁内重新核验 Run 与 Admission。变更窗口内继续暂停 scheduler 与人工准入，便于把
   dry-run、apply 和复核作为一个受控批次；恢复前必须完成下方检查。
4. 由运维环境加载 `DATABASE_URL`。默认 dry-run 可从 `TRADEOS_TENANT_ID` 读取租户；
   `--apply` 不接受该隐式租户，必须在命令行再次写出精确 tenant ID。
5. 不在聊天、终端历史、工单或报告中粘贴数据库密码、Token、密钥值、Need snapshot 或
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
不会形成 `WAITING + RUNNING`。只有领域服务 claim 后的 `STARTING` 可首次创建或恢复 Run；恢复
还必须使用同 tenant、同幂等键、同 workflow type/version/subject 的既有 Run，且不会新建第二条。
`ADMITTED` 的接口级幂等直接返回已绑定结果，不再调用 engine.start。

执行成功后：

1. 复核 JSON 计数和每个 Case 的准入详情；
2. 确认当前 active Directive 是否包含老板确认的自动准入开关与 `batch_limit`；没有配置、关闭
   或状态未知时都必须保持零 Workflow start；
3. 恢复 scheduler；观察第一轮准入数量、排序解释和 canonical Run 绑定；
4. 需要人工启动时仍走原人工准入 API 与幂等请求 ID，禁止直接更新数据库状态。

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
- 不得在回填窗口中 downgrade 0055。确需回退应用时，先停止回填与所有 V2 start，再按变更单
  评估兼容版本；移除数据库 guard 会重新开放未准入 Run 的写入窗口。

受控测试通过只证明命令与核心服务接线正确，不代表生产已执行回填、启用真实 Sourcing V2，
或授权任何外部来源调用。
