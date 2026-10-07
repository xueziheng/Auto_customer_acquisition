# Task7：收件箱当前负责人权限交付报告

## 范围与真实接口

实现基点：`97a534796944dd891f908c163d0a1cd2f88050e1`。Task6原真实入站/回复闭环保留，未做Task8页面、Task9恢复或Task12全仓门禁。

源码/spec/ADR提交：`3090ee35763b6e6f84b302d4a91715e18cb99bfc`。报告文档为随后独立提交；其精确ID在交付消息中给出（本文件无法写入自身提交哈希）。

公开接口以实际源码为准：

- `domains/conversations/service.py`显式复出口`InboxActor`、`InboxScope`、`InboxAction`、`InboxEmployeeFacts`、`InboxAccessFactsReader`。Actor是frozen dataclass，含tenant/employee/role/scope/frozenset allowed_owner_ids；Scope是SELF/MANAGER/TENANT枚举。五动作LIST/READ/CORRECT/EVIDENCE_READ/NEXT_QUESTIONS共用当前归属矩阵。
- `list_inbox(..., *, actor, category, limit)`、`get_inbox_detail(..., *, actor, action=READ)`、`correct_classification(..., corrected_by, *, actor)`、`get_message_evidence(..., *, actor, action=EVIDENCE_READ)`。缺actor不再默认为boss；corrected_by强制等于真实actor。证据返回`InboxEvidenceRef(tenant_id,message_id,conversation_id,account_id,artifact_id)`，不是任意artifact领取凭证。
- 事实port提供`read_employee`、`read_owner`、`lock_account_access`。`SqlAlchemyConversationsUnitOfWork.inbox_facts`绑定本次AsyncSession；`infra/db/inbox_access.py`仅读当前Employee和OwnershipLock的安全metadata。不通过假boss调用员工内部授权，不在会话域导入其他域。
- Repository的`get_inbox(..., actor)`及`list_recent(..., actor, limit)`是当前SQL权限入口。旧`get`只供内部工作流/Message关联解析，不对Inbox HTTP直接开放。SQL tenant/主体active+role/owneractive+直属/snapshot集合过滤先于LIMIT。
- `RequestIdentity.conversation_inbox_actor`从原受信EmployeeView及同次owner快照机械映射；notification的`require_inbox_access`与通知本人InboxActor保持不变。
- HTTP新增`GET /inbox/messages/{message_id}/evidence`，只取canonical message ID，拒绝query/body。返回octet-stream安全attachment（canonical `.eml`文件名）、`Cache-Control: private, no-store`和`X-Content-Type-Options: nosniff`。
- `workflows/reply_qualification/questions.py::ReplySuggestionApplication.read(..., *, actor)`接同一snapshot。真实manager/MANAGER及sales/SELF走Outreach `REPLY_SOURCE_READ`精确单account；下一跳Enrollment进一步收窄精确enrollment ID。SELF的account-only scope只可用于该只读action，其它原action仍要求原scope。Demand保留原公共读签名，各跳核account、来源假设和所选Message当前证据；所有成功及need_unavailable分支均在返回前再核当前权限。

## 授权、锁与最后读取点

矩阵：当前active boss显式tenant范围，包括无归属和停用owner；manager自身与活跃直属；sales自身；其它/unknown/system/停用拒绝；跨tenant拒绝。角色和请求snapshot不一致直接拒绝，不在请求途中升级权限。manager不递归孙级，不从Opportunity.owner或历史发送者推断owner。

纠正按真实Message→Conversation.account解析。先`OwnershipLock FOR SHARE`，再按employee_id升序`Employee FOR SHARE`锁actor和当前owner，锁后重验并追加纠正，提交才释放。现有transfer条件UPDATE与ownership锁冲突，现有停用/manager更新与employee锁冲突。不是KEY SHARE，不修改员工写协议。没有归属行时仅boss可核对，新的分配不改变其tenant权限。

最后自检发现实际READ COMMITTED漏洞：读到活跃manager后，另一个事务将其停用并把原boss客户转给其sales；再读新owner时，旧principal与新owner拼成从未真实存在的权限。真实多连接测试在修复前返回了证据引用（DID NOT RAISE）。修复用既有repository `get_inbox`单SQL predicate对列表每个返回项、详情和Message证据做最后资源核验并比对account。写纠正仍保留事务锁；单SQL不是锁替代。

Gateway新增独立`inbox.message.evidence.read` LOW/FREE/NONE manifest/handler，API自己的工厂装配，复用原入站组合owned bounded RawStore，不修改pipeline。受信snapshot存在task-bound ContextVar槽位，参数仅message_id，ledger仅一次性provider_ref；对象读取前、读取后、领取前均重验当前Message权限。校验EMAIL_RAW/mime/tenant/artifact/size/hash，有界内存读取；对象IO不持业务锁。原件读期间转交真实拒绝，不返回长期公链。

读取的最后授权点不等于任意时刻撤回已经合法返回的数据；列表逐资源复核不宣称全列表多语句原子快照。分类筛选仍只看最近最多200条**已授权**会话，不宣称全历史分页。当前无稳定MIME part附件接口，所以没有独立附件下载/扫描/持久化。原qualify、technical-review/retry、quotation source和通知SYSTEM窄口均未放宽。

## TDD与迭代证据（不累加通过数）

以下测试均在本worktree显式cwd执行，统一前缀：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q
```

每行列出此前缀后的精确参数；失败类型如实区分，不冒称所有失败都是产品RED。

| 参数 | exit / 实际输出 | 说明 |
|---|---|---|
| `tests/unit/test_conversation_inbox_views.py::test_missing_actor_cannot_read_tenant_inbox` | 1；1 failed in 0.15s | 首个产品RED：无actor仍返回真实域投影，未拒绝。 |
| `tests/integration/test_inbox_access.py -k 'matrix and self and sales'` | 1；1 failed,17 deselected in 7.45s | 测试前置错误：record_classification多传一个位置参数，修为真实公开签名。 |
| `tests/unit/test_inbox_api.py -k owner_roles` | 首轮2：collection NameError pytest；修导入后1：2 failed,5 deselected in 1.15s | 后一轮才是HTTP产品RED：manager/sales实际403。 |
| `tests/unit/test_inbox_api.py tests/unit/test_conversation_inbox_views.py tests/integration/test_inbox_access.py` | 1；4 failed,25 passed in 11.75s | owned角色中无viewer，测试矩阵改用真实product；另列显式其它role拒绝测试。 |
| `tests/integration/test_inbox_access.py -k evidence_gateway` | 1；1 failed,18 deselected in 6.83s | 原件插件模块尚不存在；这是缺功能接口失败，不冒称完整行为RED。 |
| `tests/integration/test_inbox_access.py tests/integration/test_reply_completion.py` | 0；42 passed in 41.66s | 中间版本真实PG/MinIO/回复闭环作用组。 |
| `tests/integration/test_inbox_access.py -k linearizes` | 0；3 passed,19 deselected in 7.60s | 纠正事务锁三种UPDATE实际阻塞。随后做撤锁回归RED。 |
| `tests/integration/test_inbox_access.py tests/integration/test_reply_completion.py -k 'gateway or http_message or other_roles or real_need_evidence'` | 1；2 failed,12 passed,41 deselected in 18.38s | 两个前置错误：不存在WEB_PAGE枚举、HTTP少原tenant header。 |
| `tests/integration/test_conversations_correction.py tests/unit/test_inbox_api.py tests/unit/test_conversation_inbox_views.py tests/unit/test_crm_router.py tests/unit/test_crm_handoff_router.py tests/unit/test_notification_api.py tests/unit/test_outreach_permissions.py` | 1；3 failed,95 passed in 9.24s | 两个旧unit fake误改签名、Outreach预期矩阵尚未更新；真实旧纠正组当轮通过。 |
| `tests/integration/test_inbox_access.py tests/integration/test_reply_completion.py tests/integration/test_conversations_correction.py tests/unit/test_inbox_api.py tests/unit/test_conversation_inbox_views.py tests/unit/test_crm_router.py tests/unit/test_crm_handoff_router.py tests/unit/test_notification_api.py tests/unit/test_outreach_permissions.py` | 1；4 failed,153 passed in 51.56s | 仍是上述原件两个前置及旧unit classifications.get误重命名。 |
| 下述最终组（补scope测试、单SQL反例尚未添加） | 1；1 failed,177 passed in 60.25s | WEB_SNAPSHOT必须带其合法text/html，错误MIME在原RawStore门就被拒；修测试前置。 |
| 同上组 | 1；8 failed,170 passed in 62.25s | 结构检查后把旧correction身份创建迁原initialize_identities，误将owned两位boss映成同一employee_id；非产品RED。 |
| `tests/integration/test_conversations_correction.py` | 0；12 passed in 7.94s | 按boss序号生成两个真实唯一身份，原公开initializer+ingest前置通过。 |
| `tests/integration/test_inbox_access.py -k combine_old` | 1；1 failed,38 deselected in 7.53s | 实际分次事实拼接漏洞：应拒绝却返回Message证据引用。 |
| `tests/integration/test_inbox_access.py -k 'combine_old or linearizes or wins_before'` | 1；1 failed,6 passed,32 deselected in 8.33s | 首次文本补丁未匹配格式、assertion终止且未改源代码，随后命令仍是RED；如实保留。 |
| 同上一条精确参数 | 0；7 passed,32 deselected in 8.09s | 最小单SQL返回前复核后GREEN；锁两个提交顺序同时保持通过。 |

撤锁回归RED命令：`.venv/bin/python /tmp/task7-lock-red.py`。该临时脚本只把本批新增`if lock:`临时改成不执行，保留现有当前事实检查；以finally恢复原文件，内部执行上表统一前缀加`tests/integration/test_inbox_access.py -k linearizes`。exit1，**3 failed,19 deselected in 7.29s**。三种真实UPDATE均未阻塞，测试的第三连接通过`pg_blocking_pids`观察实际PG竞争；不是单连接savepoint、仅sleep或业务替身。恢复后的GREEN与最终组均仍检查锁。

早期边界检查曾发现两个测试直接import employees.models，exit1；未放宽checker，改用原公开`initialize_identities`及已获取员工实体的类型构造更新，已移除这两个import。Mypy中间报SQL bool表达式和ArtifactId类型两项，修为typed SQL false及显式ArtifactId后通过。Ruff中间修导入/公开复出口noqa及fixture F811，未关闭生产规则。

## 最终同版本作用组

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/integration/test_inbox_access.py tests/integration/test_reply_completion.py tests/integration/test_conversations_correction.py tests/unit/test_inbox_api.py tests/unit/test_conversation_inbox_views.py tests/unit/test_crm_router.py tests/unit/test_crm_handoff_router.py tests/unit/test_notification_api.py tests/unit/test_outreach_permissions.py tests/unit/test_reply_runtime_binding.py tests/integration/test_email_inbound_access.py
```

最终结果：**exit0，179 passed in 63.59s，无skip**。该组对应最终源码；已生成类型与当前API/DTO一致，之后只归档文档，不再改生产/测试代码。此前通过数不累加。

覆盖角色/归属/action矩阵、未知/system/其它role拒绝、停用owner、manager孙级及scope上界、升降职旧snapshot、201条越权最新会话占窗、伪造scope/跨tenant/纠正人、原件安全响应与禁止artifact覆盖、对象读中转交/跨tenant/错kind、双向transfer/停用/直属多连接竞争、READ COMMITTED不一致反例、实际需求下一问staff整链和中途转交/早退后复核。旧原判保留、纠正幂等和无事件重放、Inbox/CRM/本人通知、Outreach action隔离及Task5技术review原权限均保留。

## 静态、schema与清理

最终Ruff精确文件组：

```sh
.venv/bin/python -m ruff check apps/api/composition/runtime.py apps/api/dependencies.py apps/api/identity.py apps/api/inbox_evidence.py apps/api/routers/inbox.py domains/conversations/inbox_access.py domains/conversations/repository.py domains/conversations/schemas.py domains/conversations/service.py domains/conversations/service_impl.py domains/outreach/permissions.py infra/db/conversations_uow.py infra/db/inbox_access.py infra/db/repositories/conversations.py tests/integration/test_conversations_correction.py tests/integration/test_inbox_access.py tests/integration/test_reply_completion.py tests/unit/test_conversation_inbox_views.py tests/unit/test_inbox_api.py tests/unit/test_outreach_permissions.py tool_gateway/handlers/inbox_evidence.py workflows/reply_qualification/questions.py
.venv/bin/python -m mypy domains/conversations infra/db/inbox_access.py infra/db/conversations_uow.py infra/db/repositories/conversations.py apps/api/identity.py apps/api/routers/inbox.py apps/api/inbox_evidence.py apps/api/dependencies.py apps/api/composition/runtime.py tool_gateway/handlers/inbox_evidence.py workflows/reply_qualification/questions.py domains/outreach/permissions.py
.venv/bin/python scripts/check_boundaries.py
```

最终三个命令分别exit0：Ruff `All checks passed!`；Mypy `Success: no issues found in 21 source files`；边界七项全部通过。`scan_sensitive.py`使用上列22个Python文件加本批spec/ADR，已实际exit0无命中；末次source修改后再次扫描，exit0无命中。`git diff --check`前轮exit0，stdout空、stderr安全捕获32行AppleDouble噪声；未repair/repack/delete共享.git。

Schema exporter、generator独立命令与exit（不是pipe隐藏失败）：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python apps/web/scripts/export_openapi.py > /tmp/task7-openapi.json
node apps/web/node_modules/openapi-typescript/bin/cli.js /tmp/task7-openapi.json -o apps/web/src/api/api.d.ts
```

exporter exit0；generator exit0，openapi-typescript 7.13.0成功生成。TS命令在显式`本worktree/apps/web`执行`node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`，exit0无输出。最后单SQL改动不改变API/DTO；没有重复无关Web lint/build。只生成api.d.ts，不改页面。

所有PG/MinIO沿原Supervisor owned fixture，dotenv禁用，TEST_DATABASE_URL清除。原件/actor/归属通过原public initializer、服务、仓储和RawStore形成，审批/Need/出站终态没有seed。真实模型/Gmail外部端口仍受控，原领域/engine/Gateway/PG/MinIO真实。fixture finally检查owned清理，runtime engine/store关闭；未用旧DSN、读.env、复用他人数据库、输出凭证、pkill/prune、修改原目录/Catalog/shared.git。临时文件只在本批/tmp，git stderr仅计数。未启动任何子代理。

## not_run与Task8交接

- 没有真实模型/Gmail/客户发送、push/merge/deploy；没有浏览器页面或独立附件能力验收。Task8消费本批真实API与生成类型。
- 全仓门禁/其它历史testcontainers组、Task12 HTML void Minor和既有Git侧文件噪声不属于本批。没有新增迁移。
- 列表分类是最多200条授权会话窗口；不存在未知owner员工全租户fallback；已返回数据不能撤回。
- 独立审查由controller派发，本批未自审代替独立review；交付后保持源码不变等待审查。

## 本地提交收口

源码/spec/ADR提交实际exit0，28文件；git add stderr安全捕获28行、commit stderr安全捕获571行既有Git sidecar噪声。Git还提示本机默认committer身份，未修改全局git配置、改写提交或触碰共享.git。最终同版本敏感扫描exit0无命中，diff --check exit0/32行stderr噪声；报告单独提交。所有179项、21源Mypy及schema/TS结果均对应3090ee3所含源码和生成类型；源码冻结，后续仅本报告文档提交。
