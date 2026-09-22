# 最终统一修复报告

日期：2026-09-07。实施已完成，等待原审查者一次限定复审；本报告不表示最终 Approved。

- 派发 BASE：`21d3a3bb31f2419957668cb65f9eedce613686be`。
- 精确源码提交：`02506e44b52392879ea7b8a5b350da435a9f827f`，17 个文件；源码与报告分开提交。
- 范围仅 Important R1 和 Minor M1；M2/M3 按最终审查继续 parked，没有借本波扩测。
- 无子代理、push、merge、部署、真实 Provider/客户发送、供应商联系或桌面动作。

## R1 实施与权限语义

已全文读取 final-fix-brief、final-review、根 AGENTS/HANDBOOK 与进入目录的上级/就近规则；docs 下没有 AGENTS。
最小契约事先记录于 `docs/adr/0066-reply-current-authority-and-gateway-read.md`，没有更改九条硬边界。

1. `CurrentReplyAccess` 每次经原 EmployeeService 的受信 `system:reply-actor` lookup 读取当前员工事实，再执行原 `require_reply_internal_access(action="qualify")`。system 只用于查事实；配置 tenant/employee 不是当前授权。缺失、停用、降权、跨租户统一失败关闭。Message 查询前后均重核当前资格，不将 Message 查询的 await 当作权限保持保证。
2. `GatewayReplyContentReader` 使用本进程独立的原 `InboxEvidenceHandler`、`ToolGateway`、真实 PostgreSQL ledger 和原 bounded store。复用 `inbox.message.evidence.read`，取 active boss qualify 与原 Inbox evidence 的权限交集；不扩 manager/sales，不借用 technical review_id，不改 Gateway 核心或已有 handler。原 tenant/user/message/task 绑定、EXECUTING 审计先于 IO、元数据/类型/hash/大小检查、读后当前权限、SUCCEEDED 后同调用 bytes 领取及 finally 清槽全部保留。
3. 从旧 Artifact reader 提取共同的纯 MIME→安全投影函数；原完整候选 guard、预算、当前表达/引用隔离和逐字证据不变。canonical factory 只装配 Gateway reader；旧 Artifact reader 保留供已有显式内容端口消费者使用。
4. 原 ClassifyStep 接收受信窄 `ReplyClassificationAccess`：检查 `subject_ref == message_id`，当前真实同租户入站/原出站关联，分别在 Raw 前、模型前、模型返回后重核。模型/投影校验顺序与后续动作自身授权保留，没有接管取消/超时异常。
5. `record_classification` 的受托 actor 分支在原写入事务内先复用 Inbox ownership/employee 访问锁，重核当前 active boss qualify 与真实入站/出站绑定，再拿原 Message 幂等锁并写分类/ReplyReceived。员工共享锁保持到 commit：先提交的撤权会拒绝分类；已拿锁的分类先完成，撤权等待后生效。授权先于旧分类 no-op；没有把 wrapper 检查冒称整个持久化原子授权。
6. 原 ConversationService、Outreach 和 canonical services 实例保持不变。新增 actor/access 为旧显式低层端口兼容参数，canonical factory 必定传入，没有静默降级分支。无模型/prompt/类别或财务规则修改。

## M1 实施

`infra/controlled/reply_model.py` 四处使用 `closing(self._connect())` 加原连接事务上下文。
具名 `CountedConnection` 覆盖 init、set_response、complete_json、call_count 的正常和异常路径，断言每次 close 恰好一次及原 commit/rollback 分支；原未配置响应仍计一次调用的测试保留。
没有打开原 SQLite、读取其内容或做大规模资源测试。

## 失败历史与修复验证

所有命令 cwd 均为 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`；Python 为 `.venv/bin/python`（预检 3.12.14），涉及 Supervisor 的测试只在子进程 PATH 前置 `/Users/xueziheng/.nvm/versions/node/v24.15.0/bin`。没有修改用户 shell 配置。

| 顺序 | 实际命令/选择 | 结果 | pytest 秒数 |
| --- | --- | --- | --- |
| M1 RED | `.venv/bin/python -m pytest tests/unit/test_controlled_reply_model.py -q --tb=no` | 8 failed / 1 passed，四方法正常/异常均 close=0 | 0.22 |
| R1 环境失败 | `.venv/bin/python -m pytest tests/integration/test_reply_completion.py -k current_reply_authority -q --tb=no`，未前置 Node PATH | 7 setup errors / 23 deselected；不是产品 RED。Supervisor 依赖检查需要 Node，后续用已预检路径恢复 | 3.22 |
| R1 单例诊断 | 同文件 `-k 'current_reply_authority and inactive' -q --tb=short`，经 Python subprocess 捕获只输出安全失败行 | Raw count 1，应为 0；不保存或输出原始敏感日志 | 此次未保留 pytest 时长 |
| R1 RED | `PATH=/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH .venv/bin/python -m pytest tests/integration/test_reply_completion.py -k current_reply_authority -q --tb=no` | 7 failed / 23 deselected。inactive/sales/missing/foreign_employee/after_raw/during_model/authorized；正常对照缺原件 Gateway ledger | 15.62 |
| 初次 GREEN | 两文件 `tests/unit/test_controlled_reply_model.py tests/integration/test_reply_completion.py -k 'controlled or current_reply_authority' -q --tb=no`，相同 PATH | 16 passed / 23 deselected | 22.99 |
| 附加边界首轮 | 同 integration 文件 `-k 'current_reply_authority and (mismatch or run_tenant or audit)' -q --tb=no` | 4 passed / 1 failed / 30 deselected；subject/outbound mismatch 和 executing/succeeded 审计故障通过。run_tenant 测试初稿修改 Run 的 tenant；根据 workflow_steps 复合 FK 结构，这一测试设置不合法（该轮只保留失败汇总，未输出原始异常全文），不作为产品 RED；没有因此改生产表或放宽隔离 | 11.71 |
| 分类写入等待 | 同文件 `-k 'current_reply_authority and persist_wait' -q --tb=no` | 1 passed / 35 deselected。真实第三连接观察 PG blocking；先锁员工行，分类等待 share lock 后提交停用，零新分类 | 7.80 |
| 修正跨租户测试 | 同文件 `-k 'current_reply_authority and run_tenant' -q --tb=no` | 1 passed / 35 deselected。从真实 canonical factory 取得原 access，对外租户调用直接 PermissionDenied；不再篡改耐久 Run 租户 | 7.52 |
| 最终完整受影响回归 | 下列 13 文件同轮完整运行 | **216 passed，0 failed/0 skipped；汇总未报告 warnings** | **73.69** |
| 原受控主链 | 原 `tests/e2e/test_web_core_controlled.py` 全文件，见下文仅隔离输出目录的 runner | **1 passed，0 failed/0 skipped；汇总未报告 warnings** | **35.93** |

最终受影响命令（无 `-k`）：

```bash
PATH=/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH .venv/bin/python -m pytest tests/unit/test_controlled_reply_model.py tests/unit/test_reply_runtime_binding.py tests/unit/test_reply_content_boundary.py tests/unit/test_reply_current_evidence.py tests/unit/test_composed_reply_actions.py tests/unit/test_reply_action_dispatch.py tests/integration/test_reply_completion.py tests/integration/test_scheduler_reply_trigger.py tests/integration/test_reply_qualification_workflow.py tests/integration/test_message_content_reader.py tests/integration/test_conversations_classification.py tests/integration/test_conversations_correction.py tests/integration/test_inbox_access.py -q --tb=no
```

两个最终测试进程使用独立 pytest 目录、owner、动态端口和隔离 PG/MinIO；原件/领域/Gateway/工作流为真实实现。
新 R1 用例只在实际 S3 bounded transport 包一层计数/撤权钩子，仍调用真实底层；模型为原受控计数端口。
审计故障由本 owner PG 中精确 tenant/tool/status 的真实 UPDATE trigger 产生，不 mock ledger，finally 精确移除自己的 trigger/function。
13 个本波 Python 源码/测试文件在两进程启动前保存 hash，结束后、提交前逐一相符；提交后再次与 Git 源码版本对应。

主链实际命令：

```bash
PATH=/Users/xueziheng/.nvm/versions/node/v24.15.0/bin:$PATH .venv/bin/python /tmp/tradeos-final-fix-e2e.py
```

runner 仅将原测试模块 `EVIDENCE` 改到新 `output/acceptance/final-fix-20260907/e2e`，设置 `PYTHON_DOTENV_DISABLED=1`、`TRADEOS_REQUIRE_E2E=1`，然后执行 `pytest.main(['tests/e2e/test_web_core_controlled.py','-q','--tb=no'])`。不改业务场景或断言，不覆盖历史 Task12 输出。
runner 副本、源码 hash 和静态门禁保存在本波 output 目录，可按原实际命令核验。

## 原主链、安全清理与产物

证据根：`output/acceptance/final-fix-20260907/`。

- 原主链 owner：`5be45c31379045a798f96f93226841b5`。
- `e2e/5be45c31379045a798f96f93226841b5/proof.json`：research_ready=true、合成研究 3 Signal/3 Hypothesis；真实可达性 verified、未验证入组拒绝；真实独立审批保持；reply 模型调用 1、发送调用 1；人工接管 accepted；pageerrors=[]。model_calls 仅指 reply 模型，不冒称所有模型总计或真实生产成本。
- `cleanup-verification.json`：owner stopped、cleanup_errors=[]；按记录的 PID+出生时间核对 supervisor/四进程/四 anchor，无同实例存活；两个精确容器 ID 已不存在。原 E2E finally 还断言本 owner mail.sqlite、reply-model.sqlite 及 journal/wal/shm 全部清除。
- `integration-owner-cleanup.json`：本轮完整回归四个基础设施 owner 分别为 `57ae2214344e4828839e6ba5a715b840`、`911339cd6c10479f94eb13bad0e02d5c`、`bf5320d7c51442ed97c43eca60dbb0a8`、`f1f0da2c05b1407ba4f758bff2516462`。各两个精确容器 ID 均不存在、config 已删除、cleanup_errors=[]。这类 fixture 只启基础设施，落盘 status 仍为 starting，不能把它写成 stopped；清理成立来自 fixture teardown 断言与精确 ID/文件检查。旧 testcontainers 夹具资源由其原 context manager 清理，不声称本次额外审计所有 Docker 资源。
- 进入时 381 个历史未跟踪 output 路径全部保留，见 `historical-output-paths.json`；没有读取历史 Raw/SQLite/dump 或改写历史产物。新截图只属于本 owner；UI 没有修改，未按旧截图文件名推断新的视觉覆盖。
- 只读安全 status/proof/清理元数据；ControlledConfig 的受信运行期配置只在测试脚本内存使用。没有 .env/真实密钥/DSN/Cookie、SQLite 内容或原始敏感日志回显。

## 提交前静态门禁

首次局部 Ruff 自动修复 4 个 import 排序问题；早期 mypy 8 文件及随后 62 文件、结构检查已经通过。最终冻结并暂存源码后，以下完整记录由计时 runner 生成，见 `static-gates.json`：

| 命令 | 结果 | wall 秒 |
| --- | --- | --- |
| `.venv/bin/python -m ruff check` + 本波 13 个 Python 文件（精确数组见 JSON） | exit 0 | 0.094 |
| `.venv/bin/python -m mypy apps/scheduler_worker domains/conversations workflows/reply_qualification infra/controlled/reply_model.py` | 62 files / exit 0 | 0.673 |
| `.venv/bin/python scripts/check_boundaries.py` | 七组结构检查通过 / exit 0 | 4.266 |
| `.venv/bin/python scripts/scan_sensitive.py --staged` | exit 0 | 0.235 |
| `git diff --cached --check` | exit 0 | 0.040 |

Git AppleDouble stderr 只捕获行数、不回显内容、不修共享 .git：进入 rev-parse 0/diff 3；中途 diff-stat 42/聚焦 diff 16；source add 17、staged name-list 17、diff-check 30、源码 commit 484。这些行数不是业务 warnings。
源码提交没有带入控制者正在修改的 progress.md，也没有提交 output 或此报告。

## 限制与下一步

- 等待原最终审查者限定复审。本报告不判定 Spec/Quality Approved 或可合并。
- 历史后端 9318 与 Web 411 属于旧版本证据；没有本修复 SHA 的全仓同轮绿色声明。只执行本次受影响完整文件与原完整受控主链，没有重跑无新增风险依据的 full suite。
- 不承诺跨外部 IO 的分布式 fencing：已获授权开始的模型输入无法事后撤回；模型返回后已撤权则不会新增分类。分类写入的员工锁提供本地 PG 事务顺序，不是外部撤销协议。
- 原 actor-less 低层接口/显式旧组合保留兼容性；当前 canonical factory 的受托路径始终注入 access 和 actor，不使用这些兼容入口回退。
- M2 技能资源精确边界与 M3 disabled Worker 内部 await 取消直接覆盖继续 parked，理由/触发条件见 final-review；没有改这些模块。
- 通用 Agent 生产消费者、真实 Provider、共享认证/部署、真实运营与桌面仍未验收；新主链证据不扩大这些范围。
