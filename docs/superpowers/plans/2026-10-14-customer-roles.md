# 企业客户权限与工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将已批准的公司搜索、人工分配、员工跟进权限及精简 UI 实现并验证。

**Architecture:** 扩展现有库存交付为公司取得记录，新增显式分配；通过现有客户、归属锁、验证、开发任务服务连接跟进。企业 UI 按当前角色组织入口，源管理保持平台独占。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL / Vue 3 / TypeScript。

**Spec:** docs/superpowers/specs/2026-10-14-customer-roles-design.md

## Global Constraints

- HTTP 与服务门面均校验当前身份；后台交付仍进入公司待分配池。
- 一个公司同一客户只有一个当前负责人。并发分配只有一个成功；相同分配重试幂等，已分配给他人的记录拒绝覆盖。本轮不添加转派功能。
- 各公司的取得、分配、沟通与额度独立。公共池仅保留现有公开事实，不加入公司私有沟通事实。
- 本次验证不向真实客户发邮件，不伪造政策/批准/联系人验证，不增加模型配额或进行额外付费采集。
- 模型永不接触凭证；不读取生产配置内容、Cookie 或原始凭证。所有真实外部业务动作经过 Gateway。
- 所有修改位于现有 candidate-inventory 工作树；不改原始脏工作区，不推送/合并；真实客户数据不删除。读取各目录 AGENTS.md 和 HANDBOOK.md。
- Python 使用 /Volumes/T7/Company/Auto_customer_acquisition/.venv/bin/python；提交前结构检查。API 类型从 OpenAPI 生成。

## Review Focus

- 旧身份仍有效但角色已撤销时请求不得继续取得/分配客户（Task 1）。
- 同一公司两次请求与两个并发分配不能导致重复负责人或重复扣额度（Task 1）。
- 已有客户/归属与库存映射冲突不得静默改负责人或把未验证邮箱当可发送（Task 2）。
- 员工猜测其他公司/同事 ID 或直接调用旧入口仍被拒绝（Task 2）。
- 前端切换账号、慢请求、刷新重试不得显示旧身份客户或重复提交（Task 3）。

### Task 1: 公司待分配池与显式归属

**Files:** domains/prospecting/inventory.py、inventory_access.py；infra/db/prospect_inventory.py、prospect_inventory_tables.py；新增 migrations/versions/0075_*；apps/api/composition/inventory.py、routers/prospect_inventory.py；tests/unit/test_inventory_access.py、test_prospect_inventory_api.py；tests/integration/test_prospect_inventory.py；docs/adr/0093-*。

**Interfaces:** CandidateAllocation.employee_id 改为 EmployeeId | None；新增 assigned_at/assigned_by（可空历史兼容）。InventoryService.assign(tenant_id, allocation_ids: tuple[CandidateAllocationId, ...], employee_id: EmployeeId, *, acting_employee_id: EmployeeId) -> tuple[CandidateAllocation, ...]；POST /prospects/inventory/assignments body allocation_ids（1..100、唯一）、employee_id。增加 get_allocation(tenant_id, allocation_id: CandidateAllocationId) -> CandidateAllocation | None，Scoped 按当前 owner 判权。CandidateAllocation.account_id: ProspectAccountId | None = None；infra 的 assignment_bridge.bind(session: AsyncSession, tenant_id, allocation, employee_id, acting_employee_id) -> ProspectAccountId 在同一分配事务接入 Task 2 私有客户/归属公共服务，结果写入可空 account_id；同 owner 重试可补齐历史映射。保持现有列表 cursor 和 company 去重契约。分配目标必须在当前事务验证同租户在职 boss/manager/sales；API actor 不来自 body。

- [x] 在现有测试添加：sales/manager request 被拒、撤销 boss 权限被拒；新 request 与 deliver_job 结果未分配；保留历史归属；分配同租户在职员工成功，相同归属重试幂等，冲突/跨租户/离职/员工调用均拒绝；批量全有或全无；额度不因分配增加。
- [x] 运行上述定向测试记录 RED。
- [x] 实现迁移与事务分配。公司 quota 锁/分配行锁顺序一致，HTTP 和 ScopedInventory 双层 boss 授权；员工读取自身，manager 不再扩大到下属。任务/额度/来源状态仅管理员可读（来源配置仍无企业写权，企业 UI 后续删除来源面板）。历史 employee_id 保留；新交付为空。
- [x] 运行 unit + PostgreSQL inventory 集成测试；迁移往返检查（降级遇待分配数据不能静默丢失，记录限制）；检查结构、ruff、diff。
- [x] 写 ADR0093 解释用户批准的新授权模型，更新涉及 AGENTS 描述；提交并报告下游接口与测试证据。

### Task 2: 已分配客户接通现有跟进服务

**Files:** apps/api/routers/customer_discovery.py、campaigns.py 或新增 customer_followup.py；apps/api/composition/ 新增适配器；domains/prospecting/service.py 和必要实现；domains/employees/ 与 domains/outreach/ 显式接口；对应 tests/unit、tests/integration。

**Interfaces:** 使用 Task 1 的分配事实，取得/分配记录可解析为本企业 ProspectAccount 与员工 OwnershipLock；对外客户视图包含 allocation_id、account_id、owner、公开证据、联系方式验证状态和现有跟进状态。客户跟进动作由服务端从真实归属解析 actor scope；批量选中只允许本人客户。

- [x] 先检查现有账户解析、员工手动归属、联系人验证和 enrollment 服务，选择最小可恢复的桥接适配器；不得直接跨域导入内部实现。为精确接口和事务/重试策略补充本节实施备注后开始编码。
- [x] 添加失败测试：获分配员工可读自己客户，其他员工和跨租户不可读；库存邮箱未验证不能发送；批准开发任务边界外不能启动；员工不能创建/修改批准边界；客户归属冲突拒绝。公司管理员可查看和分配公司客户，普通员工只能跟进自己的。
- [x] 实现客户详情、可用任务/就绪原因、在批准范围内加入跟进和停止操作；接通已有 verified 联系方式和 enrollment 状态，不伪造 verified/批准/邮箱配置。缺配置返回具体阻塞，保持后续真实补齐可运行。
- [x] 定向测试、结构与类型检查，更新相应权限文档并提交。

#### Task 2 实施备注（现有服务检查后确定）

- Task 1 的 assignment_bridge 在组合层实现，复用传入事务构建 ProspectingService.resolve_account 与 EmployeeService.resolve_owner；受信 actor 必须对应当前锁定的真实 boss。用 candidate 的 name/country/domain/provenance 解析账户；不猜测 entity_type、不创建假需求。已有 owner 不符则抛冲突回滚整批。不得由内层 UoW commit/close 外层 session。历史已分配且 account_id 为空的记录由 boss 同 owner 分配重试补齐，详情显示待同步而不是擅自赋予新 owner。
- 对外提供 GET /prospects/customers/access，返回当前身份核实的 role、can_search、can_assign；仅使用当前 employee，不含凭证，使生产会话与受控 dev 身份均可正确渲染导航。GET /prospects/customers 是本人分配列表，支持 limit/cursor；boss 的公司待分配池在现有 inventory 列表增加可选 unassigned_only（默认 false，只有 boss 可请求 true），在 SQL 过滤后分页，避免前 200 条全已分配时漏掉更早待分配客户。
- 对外统一 /prospects/customers/{allocation_id} 为详情，返回 allocation、account（可空）、contacts、enrollments 和可理解的 blocking_reasons；GET 无创建/发送副作用。列表复用 /inventory/allocations（员工始终本人，boss 用 mine=true 的新过滤或在新 customers 列表限定本人）；管理分配页读公司全量。
- GET /prospects/customers/{allocation_id}/follow-up 返回当前公司 active Campaign 的安全选项及该客户已验证联系人。新只读适配器只返回 tenant-bound active campaign IDs，再用真实角色+精确 campaign/account scope 调用 Outreach 公共服务，不伪造 boss。可见选项不代表可发送，Enrollment.create 仍核对真实当前资格/类别/国家/抑制/发件身份。
- POST /prospects/customers/{allocation_id}/follow-up 接受 campaign_id、contact_point_id、source_hypothesis_id、campaign_version 和原始 Idempotency-Key，返回 EnrollmentView；重复请求重放，归属必须是当前员工本人，boss 也不得替员工启动未选择的沟通。POST /prospects/customers/{allocation_id}/follow-up/{enrollment_id}/stop 使用手动停止语义，必须真实归属且 enrollment.account_id 精确相同。批量选择在前端逐客户提交，逐项展示成功/失败，不宣称跨客户原子。
- 新 follow-up application 先重读当前 employee、allocation 和 OwnershipLock；所有动作校验 caller=owner，详情允许 boss 查看本公司。Outreach 的 sales/manager 仅对精确 account scope 增加 ENROLLMENT_CREATE/STOP；不赋予 Campaign 配置权。旧 manager Campaign 改写/暂停等权限收紧为 boss-only，普通 manager 资料可见范围只限自己。
- 现有 CurrentContactEligibilityReader 依赖已验证联系方式、企业类型和有证据的需求假设类别；新公开邮箱不会自动满足这些条件。详情必须列出“联系方式待验证”“客户类型待确认”“产品适配证据待补充”“自动跟进未启用”等真实阻塞；已有合格客户可以入组，使用既有 scheduler 序列，不新增假验证、不绕过批准。不把领取本身当成沟通授权。
- 自动回复事实读取要求 Enrollment.source_hypothesis_id 非空且对应同一客户；选项必须返回真实活跃、有证据的该客户假设，启动时按公共 Demand 接口复核 account/status/category，并写入 Enrollment。必要时添加精确 account 过滤的公共读取接口，禁止先截断全公司列表再过滤；不可伪造假设补齐。缺少该事实应显示产品适配证据待补充。添加跨客户假设拒绝及来源绑定保留的回复链路回归。
- 验证桥接回滚（第 2 个客户归属冲突时第 1 个也不落库）、跨账户发送参数、版本变化、停止他人 enrollment；现有未知结果/幂等恢复保留。Task 2 新 DTO 导出 OpenAPI，Task 3 消费。

### Task 3: 精简工作台和批量客户操作

**Files:** apps/web/src/navigation.ts、components/WorkspaceNavigation.vue、router.ts、views/customer-discovery/InventoryPanel.vue、CustomerDiscovery.vue、必要的新客户工作台组件、api/api.d.ts；apps/web/tests/prospect-inventory.test.ts、workspace-navigation.test.ts 及跟进测试；apps/web/AGENTS.md、README.md。

**Interfaces:** 消费 Task 1 assignment 和 Task 2 客户跟进 API。当前角色与能力从 Task 2 的 /prospects/customers/access 读取；无身份时默认不授管理员入口。保持已有请求恢复身份绑定。

- [x] 添加角色导航、待分配批量选择、分配成功后员工可见本人客户、加载失败/切换账号状态清空、跟进未就绪说明、停止动作的前端测试并记录 RED。
- [x] 日常入口改为找客户（boss）、我的客户、消息、产品资料，管理员保留分配和必要团队/邮箱设置；隐藏供应卡、寻源、成本报价、开发任务、商机、需求证据等并列入口，详情可承接需要的相关操作。搜索页面删除来源诊断选择和技术预算展示，保留数量/搜索状态/可理解失败提示。
- [x] 完成批量分配、员工客户列表/详情及自动跟进入口，未就绪状态解释实际阻塞，不把按钮隐藏当成完成自动发信。
- [x] 生成 OpenAPI TS 类型，运行相关 frontend tests、vue-tsc/Vite build、结构检查，提交。

#### Task 3 实施备注

- 以 `/customers` 为新的默认落点，显示“我的客户”；详情使用明确 allocation ID。管理员的“找客户”沿用 `/prospects/accounts`，把搜索和“待分配客户”放在同一工作流；无需新建一套后台。员工直达该路径显示权限说明，不能挂载 InventoryPanel 或发起公司搜索/来源请求。
- 主导航的找客户/企业管理入口按 access 的 can_search/can_assign/当前角色显示；我的客户、消息、产品资料为常规入口。旧审批、接管、商机、需求等深链仍能解析到对应分组，但不重新放回并列主菜单。保留管理员从客户跟进处进入开发任务配置的必要链接。
- 移除搜索页重复的旧“潜在企业与联系人”大面板；资料及验证状态承接到新客户详情。保留可追溯公开来源、明确未验证标记、公开邮箱不得被计为已验证；隐藏平台源、技术预算、公共库跨市场总数，保留本公司数量/额度与可理解任务状态。
- 公司待分配列表使用 unassigned_only 的服务端分页，员工下拉来自当前企业在职员工 API；选中数、目标员工、提交结果清楚可见。分配成功清除已处理选择并刷新。已分配给他人的客户不提供覆盖/转派操作；历史已分配缺档案映射时，管理员可显式同负责人“关联客户档案”，调用既有幂等分配入口，不声称联系人已验证。
- 新客户列表、详情和跟进选项统一使用已有请求身份/代际保护，换账号或 403 清除旧资料、选择、表单、待操作；慢请求不得覆盖新身份。批量跟进按每位客户实际选定的任务/已验证联系人/来源假设组成原始命令，未知结果保留原键供核对，刷新或切换账号不偷偷重发。
- 搜索按钮用“开始搜索”，交付成功明确“进入待分配”；失败历史收进“最近搜索”可展开区域，当前进行中/结果未知的请求保留明显状态及原请求恢复。不能把旧的多条失败任务长期铺满主界面。分配一次最多选择100条，分页全选需明确是当前已加载范围，不能假称全公司全选。
- 不建立新通用设计系统，延续现有字体/色彩/间距；桌面及 390px 检查列表、操作条和表单不溢出。所有wire类型来自生成的 OpenAPI；可以复用现有useQuoteRequestScope作为请求保护，无需为命名改造无关组件。

### Task 4: 总体验证、升级与真实 UI 验收

**Files:** docs/acceptance/2026-10-14-customer-roles.md 与非敏感截图。

- [x] 独立全分支审查及覆盖风险的集成测试；检查迁移单 head、已有归属不变、企业隔离和权限降级。
- [x] 使用既有正式发布流程建立新 release、备份并迁移、切换服务；不要操作 /srv/tradeos/development/TradeOS，不读取凭证；保留可恢复发布。用户已授权服务器实现与 UI 验证。
- [x] 在 jslt 的真实 Chrome 会话验证管理员搜索入口、分配列表和简化导航；员工用已有授权会话或隔离测试验证，不获取密码。会话过期则请用户手动登录，独立验证继续。
- [x] 验收报告区分本地通过、生产已验证与需要补配置的通信能力；不向真实客户发送邮件；提交文档并给出简明结果。

验收完成于2026-10-14：代码57f2e21已部署，最终独立复审Approved；详见[验收记录](../../acceptance/2026-10-14-customer-roles.md)。真实发信和额外采集未执行；实际配置缺项如实保留。
