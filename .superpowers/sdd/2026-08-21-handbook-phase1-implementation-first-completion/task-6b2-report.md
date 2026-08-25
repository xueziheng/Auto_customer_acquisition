# Task 6B2 实施报告

日期：2026-08-25
实现提交：`44844a04fffd0593258cd1b5891bc579ef6dbe2b`

## 结论

Task 6B2 的 Campaign、回复确定性动作、字段 Provenance、handoff 组合与受控
eval corpus 验收已在本地 synthetic data 和 disposable PostgreSQL 上完成。没有
调用真实模型/provider、Gmail 或客户发送，没有网络、部署或 push，也不据此声称
Phase 1 已可运营。

真实 provider/model 评估状态：**`not_run`**。仓库当前没有可用的生产
`ReplyModelPort` provider/凭证，本任务也禁止真实外部调用。通过的是 160 条 corpus
完整性与 keyword baseline runner smoke；smoke 指标不得视为生产模型验收。

## 既有覆盖复用

先盘点并复用了既有测试，没有复制历史聚合文件：

- Campaign 当前版本审批、revision 后重新审批、pause/resume、daily limit 与发送前
  stop-on-reply race：Campaign lifecycle/workflow/scheduler driver 测试。
- exact 14-category enum、完整 `REPLY_ACTIONS` 映射、unsubscribe stop+suppress、
  auto-reply 零动作：reply action/eval integrity 测试。
- handoff 当前 owner、完整 packet、requested_at/SLA、pending handoff 幂等与队列：
  Opportunity/human-handoff 测试。
- hard bounce/complaint 的 tenant/resource binding、发送身份 reputation、抑制、停序列、
  outbox、事务回滚与重放幂等：Outreach、Sending Identity、email-feedback 测试。

最初选择集为 54 个既有测试，全绿后才新增缺口覆盖。

## 修复的真实缺口

1. `ReplyQualificationComposition.action_ports` 原为 optional，生产可在动作未接线时启动，
   直到客户回复才失败。现在启用 reply flow 必须在装配期提供完整端口。
2. `ClassifyStep` 丢弃模型已逐字校验的 `candidate_fields`。现在证据随
   `conversation_classifications` tenant-bound 耐久化；0035 增加 JSONB array 列，
   workflow context、outbox 与日志仍只含 metadata。
3. 新增生产 `ConversationReplyEvidenceReader`，崩溃后只凭 tenant/message ID 重读
   分类、模型版本、候选字段与 artifact 引用；跨租户不可见。
4. 新增 `ComposedReplyActionPorts`：
   - extract 通过 DemandService 更新 need 或晋升 hypothesis；值、message provenance、
     模型版本和逐字 quote 一起保存；相同重放 no-op，冲突 fail-closed；
   - handoff 通过 OpportunityService 创建完整 packet；客户原话只进入业务
     Provenance/handoff，不进入 workflow/outbox/log；OpportunityService 在事务内读取
     current owner，并生成 requested timing；
   - bounce/complaint 先按 enrollment 读取 sending identity，再以 identity-scoped actor
     和 RFC Message-ID 精确 resolve delivery target；同一确定性 SHA-256 dedup 同时写
     Sending Identity reputation 与 Outreach suppression/stop/event，重放不重复；
   - 删除可静默 no-op 的万能 auxiliary 委托。
5. `requests_materials` 原会错误降级为 `agent_low_confidence` handoff trigger；现新增明确
   `materials_requested`。materials/quote/sample 请求即使没有提取字段，也会按类别请求
   handoff；缺 business/opportunity mapping 时固定 fail-closed，不猜机会。
6. `Provenance` 增加可选非空 `source_quote`，Need repository roundtrip 与查询 DTO 不再
   丢失客户逐字证据。

## 14 类动作矩阵

| 类别 | 当前确定性结果 |
|---|---|
| clear_interest | stop_sequence；start_qualification 无公共写 API，固定 fail-closed |
| willing_to_continue | stop_sequence；start_qualification 固定 fail-closed |
| requests_materials | stop_sequence + `materials_requested` handoff |
| requests_quote | stop_sequence + `quote_requested` handoff |
| requests_sample | stop_sequence + `sample_requested` handoff |
| provides_specification | stop_sequence + provenance extract + specification handoff |
| no_current_need | stop_sequence；future restart 无公共写 API，固定 fail-closed |
| future_need_possible | stop_sequence；follow-up task 无公共写 API，固定 fail-closed |
| refers_other_contact | stop_sequence；referral intake 无合法结构化联系人输入/公共写 API，固定 fail-closed |
| rejection | stop_sequence |
| unsubscribe | stop_sequence + contact suppression |
| bounce | correlated hard-bounce reputation + suppression/stop |
| auto_reply | 零动作、不停序列 |
| complaint | stop_sequence + suppression + correlated complaint reputation/event |

这里的四个 `fail-closed` 是明确、固定、零下游写入的不可用状态，不是 placeholder
成功；因此不能把这些类别报告为已具备后续业务执行能力。

## TDD 证据

见证的主要 RED：

1. 字段证据测试得到 `candidate_fields=None`；证明 workflow/仓储丢字段。
2. 缺 action ports 的生产组合测试 `DID NOT RAISE`；证明 optional 装配缺口。
3. Need quote provenance 测试因 `{value, quote}` 形状不被接受而失败。
4. 生产 composer 测试因模块不存在失败；随后去除 auxiliary no-op 时新选择集
   `8 failed`（旧构造不接受真实 Outreach 依赖）。
5. 0035 roundtrip 测试因 ORM 缺同名 JSON-array CHECK 得到 `KeyError`。
6. 生产 evidence reader 测试因模块不存在失败。
7. materials/quote/sample 无字段 handoff 测试为 `1 failed, 2 passed`；materials 实际被
   错标为 `agent_low_confidence`。
8. bounce/complaint reputation 组合测试 `2 failed`；证明 composer 尚未接
   SendingIdentityService，而不能借用另一条 email-feedback 路径的覆盖冒充完成。

以上每项均在最小生产修改后见证 GREEN。

## GREEN 与门禁

- composer 最终窄测试：`12 passed`。
- Campaign/reply/handoff/eval/provenance/migration 受影响聚合：
  `324 passed in 34.17s`（`-W error`）。
- 加入直接 reputation 写入后的 reply/outreach/sending-identity/email-feedback 回归：
  `116 passed in 14.38s`（`-W error`）。
- Campaign/reply/handoff/delivery-feedback/eval 较早完整选择集：`166 passed`。
- 0035 与 0018 migration roundtrip：`2 passed`；验证
  `head → 0034 → head`、JSONB/not-null/default/CHECK/ORM parity。
- corpus runner smoke：160 cases、0 errors；keyword baseline accuracy 0.762、
  unsubscribe recall 0.950、auto-reply false-stop 0.000、complaint recall 0.800、
  extract recall 0.000。以上仅用于证明 runner 诚实工作，不是 provider acceptance。
- Ruff：全部 changed Python，`All checks passed!`。
- configured mypy：`355 source files`，无问题。
- `scripts/check_boundaries.py`：全部七项通过。
- `scripts/scan_sensitive.py`（工作树显式路径及 staged blobs）：exit 0。
- `git diff --check` / staged diff check：exit 0。

## 剩余风险

1. `ReplyBusinessFactsReader` 仍是部署层 tenant-bound projection seam；当前数据模型没有
   outbound enrollment → hypothesis/need/opportunity 的唯一公共映射。缺失或歧义时
   extract/handoff 会固定失败，不按 account 猜测。这需要后续用明确关联模型完成。
2. start qualification、future restart、follow-up task、referral intake 四项没有合法
   公共领域写 API，当前明确 fail-closed。要让四类可运营，必须先设计 tenant-bound、
   幂等且有结构化输入的业务事实/任务端口；不能恢复 no-op auxiliary。
3. keyword smoke 对 unsubscribe/complaint 并非零漏判，且不提取字段；真实 provider
   acceptance 必须在 provider 可用后独立运行并保留模型版本结果。
4. 0035 上线前仍需按运维流程备份并在目标环境做迁移演练；本报告只证明 disposable
   PostgreSQL 往返。

预存未跟踪的 `apps/web/node_modules` 符号链接未纳入任何提交。
