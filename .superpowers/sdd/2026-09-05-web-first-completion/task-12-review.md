# Task12 独立审查（最终）

## 最终双结论

日期：2026-09-06。同一审查者 `/root/review_task12`，限定复审 round 1/5。

- **Spec compliance：Approved。** I1 已由真实提议人、自批拒绝、另一当前老板经原 HTTP 批准及精确 Campaign 绑定修复。A1–A10 在正式记录的受控范围、Mac/Linux 分环境与验证版本分栏内满足本批要求。
- **Code quality：Approved。** 限定修复未新增阻断问题。当前 Critical / Important / Minor 未关闭问题均为 0；初审唯一 Important/P2 I1 已关闭。

这是 Task12 的独立结论，不代替最终全分支审查，不宣布 Task13、多用户生产认证、部署或真实运营完成。

## 限定复审范围与版本核验

已完整读一次 `review-48e4465..7e10383.diff`，仅两个测试文件、52 insertions / 7 deletions；未重读原完整 diff。已读最终 task-12-report、正式验收文档新增结论及安全 JSON，并检查报告/证据提交包的文件清单。最终报告 HEAD 经只读核实为 `5b63744bc019dff6b1255cfde472caa30a32c85f`。

程序逐项校验 `final-source-snapshot.json` 的 1630 个源码 SHA256 与冻结提交 `48e4465fc307212e794d6ed87501cb418f74d245` 的 Git blob，差异为 0；当前工作区与该清单仅 `tests/e2e/test_web_core_controlled.py`、`tests/e2e/web_core_contacts.py` 两项不同，且均精确匹配 `i1-source-commit.json` 的 `7e10383c253df4a98cd224fb7ee526d721476f9a` 修复哈希。此次读取 Git 批量对象 stderr 只计 7630 行，不回显内容、不维修共享 `.git`。

因此生产代码、共享 fixture、迁移及旧作用组与第二完整门禁一致。I1 仅调整独占主链中的审批身份/断言与 proof，没有引入新的跨测试组合状态；限定完整主链回归足以验证本修复，无需第三次全后端。正式报告已清楚区分 **48e4465 的全量** 与 **7e10383 的修复后主链**，没有宣称 7e10383 已单次全量通过。

## I1 关闭证据

`tests/e2e/web_core_contacts.py:122` 起的修复保留真实 boss 创建/提交 Campaign，并将 Approval.proposed_by 绑定同一人。原客户端自批必须 HTTP400 / `request_rejected`，随后原 GET 证明 pending、无决定人、当前人不可决定；另一老板通过原 HTTP 获得 approved。激活后再核精确版本、approval ID、change_set_ref 与 approved_by。

安全 proof 的 owner 为 `8d234c0956fb498e9f171342c3b72f51`：Campaign `cmp_01M1V7H2KYG3YR28747K9S2DVE` v1 / Approval `apr_01M1V7H2PMCX7DXRRCA503FK91`，created_by=submitter=proposed_by，decided_by=campaign_approved_by 且不同于提交人；自批400后pending，独立批准后approved。原联系人 Run `run_01M1V7H2TMMDM5ATD2W1HJ1SJ8` 产生持久 verified，enrich/verify/send 各1；未验证入组拒绝，回复形成精确 Need/Opportunity/Handoff，重放/HUP后无重复发送，接受接管=true，pageerrors=[]。

修复后完整主链 1 passed / 34.97s，安全提交索引与源码哈希一致。两次 RED 的自批200与旧不成立的“独立审批”声明在最终报告保留并明确撤回，未以全量绿色抹去该缺陷。`i1-static-gates.json` 的 boundary/scan均exit0，最终报告另记录受影响ruff通过。

## 最终门禁与 A1–A10 证据

- `backend-final.json` 与 `backend-final-safe-summary.json`：48e4465 全后端 exit0，9318 passed / 0 failed / 0 skipped / 0 warning entries；pytest2132.64s、wrapper2139.76s。命令清除 TEST_DATABASE_URL、禁 dotenv、required E2E启用；整轮安全摘要 changed_source_files=[]。首轮98 failed / 9217 passed仍是历史失败，未累计局部数量。
- `backend-resolution-index.json`：98条、98唯一索引及98个当前测试节点，全部 final_full=48e4465/passed，无pending。其具名隔离修复及Linux readiness组合已由第二完整门禁覆盖。
- A1/A8：先前已核新声明Python环境隔离及独立npm ci/build安全结果；Web411项及test/typecheck/lint/build/gen:api exit0、API无漂移。lint-attribution安全索引记录112 warnings/0errors，五文件合计112，各归属提交早于计划branch base。历史未保存类别的3/2 warnings保持未知。
- A2/A3/A4：最终owner研究未授权场景 Run failed、calls[]及业务副作用0；授权后研究3 Signal/3 Hypothesis，research usage/search/page各3、research model1，与单独获批触达场景分开。I1修复后真实审批→联系人验证→发送→回复→Need→接管链如上，未种业务结果。
- A5/A6：原未知发送逐次调用、Settings内部candidate commit后start失败与同actor/key/payload恢复、同端点PG pause/unpause以及真实PG各角色权限作用组包含于完整门禁。修复后主链仍执行重放/HUP、原件下载、接受和当前员工停用拒绝；没有改动这些断言。
- A7：第二完整门禁内独立Linux browser owner `2ea6b4a4e32f429192c2d1926a2ebfc1` 的 lifecycle code0/cleanup_verified=true；独立integration owner `f9aa1aec503442a08e00d81fc8ea5801` 同样成功。原公开回复/来源/单位/Decimal成本/独立审批/PDF及寻源适用链维持原断言。Mac报价未配置限制不拿503抵充Linux成功。
- A9：最终Mac cleanup stopped/errors[]，精确资源审计无匹配PID、容器或开放端口；第二全量五Linux owner审计均PID/容器/网络/端口为空。两次主动故障初code2/cleanupfalse仍保留，后续无残留没有倒写初始状态。
- A10：初审已核具名外部允许清单与真实Gateway路径；相关未知输入、空表及坏账本反例进入完整门禁。最终报告明确真实外部能力not_run、联系人限流与研究持久quota及调用账本分开。

本审查者实际查看最终Mac `need-1440.png`、`handoff-390.png` 及上述Linux `quote-390.png`、`exact-cost-1440.png`：证据摘录/原件入口、窄屏待接管队列、批准V2及精确成本来源可读，无可见横向裁切。Handoff截图是待接管队列，接受结论只引用实际测试/proof；报价截图不当作PDF内容证明。

## 最终 Cannot verify 与保留边界

本次只读核对源码与安全执行证据，未重新运行测试/浏览器/数据库/资源审计，未读取敏感配置、凭证、SQLite内容或原始敏感日志；PDF下载/解析/撤权取原完整测试结果，审查者未另行打开PDF。早期3/2 warnings的具体类别未保存，无法追溯补齐。

Mac原入口仍无完整quotation/自动寻源准入；Linux与Mac是不同owner/Need，不能合称单环境完整报价。DB stop/start随机HostPort变化不属四应用HUP透明恢复，pause/unpause仅证明固定端点短断；早期取消等待暂停PG、误重叠启动及初cleanup_unknown历史均保留。联系人秒/分钟内存限流不保证跨重启额度；reply model1不等于总模型调用；真实token、归因成本与人工处理工时未知。真实邮箱/模型/联系人/供应商验证、共享部署与生产认证均未运行或不在本批范围。没有因最终Approved把这些能力称为可用。

以下保留初审时的历史记录；其中 Requires changes 与 pending 为当时状态，已由本节最终结论及 I1 关闭记录取代。

---

## 初审历史：代码预读阶段

日期：2026-09-06。唯一审查者：`/root/review_task12`。
BASE：`6cdb40d8019d560d1490925df72a58d14f4881d6`。
冻结 SOURCE HEAD：`48e4465fc307212e794d6ed87501cb418f74d245`；本阶段只读 `git rev-parse HEAD` 核实相同，stderr 0 行。

## 双结论

- **Spec compliance：Requires changes。** A3 当前场景错记审批提议人，未证明真实独立审批（I1）。第二次完整后端仍在运行，最终报告与完整证据未到齐；不能给完整 Approved。
- **Code quality：Requires changes。** 新增验收测试存在 I1，须修复以避免绿色结果掩盖审批证据错误。已审生产增量未发现另一个具名阻断缺陷；这不替代最终完整证据核对，也不是全分支审查。

## 问题

### I1 · Important / P2：A3 错记提议人，原 Campaign 提交人实际自批

位置：`tests/e2e/web_core_contacts.py:130`（相邻决定位于 133–135 行）。

同函数 104–122 行以 `boss_identity` 创建并提交 Campaign；原 Outreach 服务把该 actor 持久记录为 Campaign 及其版本的创建人，并记录提交 action。随后新测试却把审批的 `proposed_by_employee` 写成 `config.identities[2].employee_id`，再由原 `boss_identity` 决定批准。这个不同身份没有创建或提交这份 Campaign。

原 legacy Approvals `submit` 接收调用者提供的提议人，不反查 Campaign 真实创建/提交人。因此本例虽经过真实服务和数据库，通用自批检查面对的是错误归属，绿色结果并不能证明独立审批。正式 A3、受控场景不得自报审批 actor 的要求及“审批事实不能由 fixture 绕过”的验收前提尚未满足。问题定位在新增验收编排；本次不把原 legacy 域合同扩大认定为新的生产实现缺陷。

建议：审批包记录实际 `boss_identity`，先经原审批 HTTP 证明该老板自批被拒，再让另一名当前活跃老板（已有 `config.identities[1]`）经同一 HTTP 端点批准精确包。核对 Campaign 版本/审批 ID、提交人和决定人，再推进 activate、原联系人 Workflow、验证入组、发送与回复接管。证据应保存实际角色及对象绑定，不能只改报告措辞。

验证来源（仅一次具名外部合同核对）：`domains/outreach/service_impl.py:361`、`:421`、`:513` 和 `domains/approvals/service_impl.py:715` 的 create/submit/activate 与 legacy submit 实现；没有启动测试。

## 本阶段审查方法与已核事项

已读根及本次所涉目录生效 AGENTS、HANDBOOK、Task12 brief/review-context、正式子规格与原设计 A1–A10、正式验收文档及当前仍属过程记录的 task-12-report。

已按既有审查包 1–4521 行顺序完整读一次（47 文件，2504 insertions / 92 deletions），未重生 diff；随后仅为具名风险补读合同或精确行号。没有派子代理、提交、启动 pytest/浏览器/数据库或业务进程，没有读 `.env`、真实配置/DSN/凭证、SQLite 内容或原始敏感日志。唯一写入是本审查报告。

| 文件/具名风险 | 核对结果 |
| --- | --- |
| `apps/scheduler_worker/bootstrap.py`、`contact_binding.py`、`runtime.py`：late factory 是否另建 canonical 业务实例 | Factory 与静态 contacts 互斥，显式要求 contacts/Campaign，缺 Outreach 或返回非法 ports 拒绝。runtime 传入自己的 sessions/tenant/tool user/fingerprints/lease/clock；AccountDiscovery 继续绑定 core.prospecting，且 runtime 有实例一致性检查。原静态路径保留。 |
| `apps/scheduler_worker/controlled_contacts.py`：是否用假 stage 或假 Hunter readiness | 注册原 enrich/verify manifest、handler、typed slot 与原 tenant/permission/Playbook/国家政策/suppression/rate-limit；ToolCall UoW 使用同 runtime PG sessions。受控 provider 独立，不写 Hunter ready；未改生产 Hunter readiness。 |
| 同文件的 `InMemoryHunterQuotaGuard`：是否替换了原持久额度 | 定向核 `apps/scheduler_worker/hunter_contacts.py:205` 和 `tool_gateway/checks/contact_provider.py`，原 Hunter 组合也默认使用该秒/分钟单 worker 限流器；新路径没有放宽该原机制。不列新缺陷。但这不是跨重启持久额度，须与原研究 PostgreSQL quota、持久 ToolCall ledger 分开描述。 |
| `workflows/account_discovery/steps.py:308`：可达性是否由 fixture 直接种入 | 原 VerifyContactsStep 消费 typed verifier 结果后调用 Prospecting record_verification；新增 helper 只造人工输入与未验证负例，不直接写 VERIFIED。主体完整链证据仍受 I1 限制。 |
| `apps/scheduler_worker/account_discovery.py:85`：workflow 自报 boss 是否直接授权 | 原 resolver 按 tenant 读取当前活跃员工并精确匹配 acting UserId，要求唯一 boss；新 factory 使用原 resolver，未将 UserId 字符串直接当 EmployeeId。 |
| `apps/scheduler_worker/controlled.py`、`bootstrap.py`、`infra/controlled/research.py`：研究是否只翻可用开关/种结果 | 复用原 ResearchRuntimePorts、Directive task reader、DemandIntelligenceAgent、canonical domains、RawArtifact 与原 WebDiscoveryToolComposition。仅搜索/页面/模型响应合成；明确三条查询/URL/payload/model允许清单，没有网络 fallback。原 DeferredS3 transport 由外层 runtime context finally 关闭。 |
| `tests/e2e/test_web_core_controlled.py`：research_only 是否自动转 Campaign | 配置前真实 Run failed 且 search/page/research model/业务副作用为零；配置后研究 Signal/Hypothesis，再单独人工输入触达任务。发送前断言 contacts 调用为空，研究成果不直接升级 Campaign。最终仍断言 research_ready，未把 disabled 分支当通过。 |
| `infra/controlled/providers.py`、新 research/contact 调用记录 | 原 mail SQLite 可以先有研究表；仅精确 sqlite_master 缺 provider_calls 表返回空，坏列/坏数据库不吞错，反例保留。研究逐次操作计数与 Gmail 每次实际 send 计数分开；模型总调用/token 不可由 reply 单计数推出。 |
| `connectors/gmail/inbound_mime.py`、`test_reply_current_evidence.py` | 完整标准 void 集合最小扩展；普通/自闭标签均验证引用后当前字段保留、引用内容排除及两侧不拼接。未扩写解析策略。 |
| `scripts/controlled_web_supervisor.py`、cleanup 测试 | 先停止应用，再精确清理同 owner mail/reply-model quartet；停止不确定保留私有文件，容器/listener清理仍尝试。无目录扫描、无跨 owner 删除。 |
| `test_web_core_settings_recovery.py` | 故障位于真实 candidate commit 后原 workflow.start；原 actor/key/payload 新 API 实例恢复，版本/Run 唯一。DB pause/unpause 恢复原端点，不等同 stop/start 后随机端口透明恢复。 |
| Linux fixture readiness 与 slice4 兼容 | readiness 等待原公开 wait 首次调用并保留 interval/stop/cycles>0；不改变生产 ready 或 relay容量。slice4只允许两次精确入站503与一次prepare409，并断言诚实错误 UI，未宽泛吞503。 |
| 旧迁移、API、权限、bounded fixture兼容 | 定向改 head、隔离一个持久前置测试、补当前 actor/公开 DTO 与 bounded transport，原业务拒绝/精确状态断言保留；未放宽生产迁移或权限。静态扫描 fixture 改明确 placeholder，保留完整 credential marker 与原拒绝/脱敏断言，scanner未改。 |

## 证据核对与 Cannot verify

已读安全 JSON：`final-static-gates.json` 中 boundaries/scan/ruff/mypy 均 exit0；`web-gates.json` 的 test/typecheck/lint/build/gen:api 均 exit0；`diff-gates.json` API无diff及diff-check均exit0，记录 stderr分别2/44行。`installation-isolation.json` 记录Python3.12.14、isolated=true、catalog_dependency=false；`node-isolated-final.json`记录独立npm ci/build exit0。这些是实施者保存的执行证据，审查者未重跑。

完整后端首轮 `backend-full.json` 为 exit1；过程报告明确 98 failed / 9217 passed，后续局部数量不得累计为完整通过。第二次完整门禁于冻结 48e4465 正在运行，最终结果、warnings/skips、最终清理与对应新 owner 的主链/独立 Linux 证据，均等待 followup。

本阶段未核最终截图/PDF或最终来源快照与运行版本绑定；后续仅核新增安全证据，若有修复则只核限定 fixdiff 和受影响验证，不重读原完整 diff。

保留限制：Mac 原入口未配置完整 quotation/自动寻源准入；独立 Linux 来源/单位/成本/独立审批/PDF 链具有不同 owner/Need，不合并成 Mac 完整报价。固定业务时钟不是实际耗时。DB stop/start可换公开端口，pause/unpause只证同端点短断。早期取消等待暂停PG、误重叠启动与清理历史不得删除。输入/外部模型是合成证据；真实模型、邮箱、联系人、供应商和生产账户均 not_run。未知成本/token/人工工时不记0。

后续完成条件：I1 修复并补真实完整主链证据；收齐并核对第二次完整后端及最终报告，精确声明冻结基线与限定修复后的验证范围。在此之前，本报告保持 Requires changes，不能用代码预读结束代替 Task12 最终 Approved。
