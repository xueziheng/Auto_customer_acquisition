# Task 9 中文子规格：错误、暂停与恢复

基点：91c77754e295f5eb9754bd87e4e4f920985ae2fe。只修改现有 Web 错误与恢复体验，不重做 Task 0–8，不扩成本跨页、指标或全仓验收。

## 状态与合法操作

| 页面 | canonical 读取 | 错误、等待、恢复 |
| --- | --- | --- |
| Run | GET /runs、/runs/{run_id} | 加载、成功空集与读取失败分开；401身份失效，403仅老板可读，404安全缺失，503不可用。缺失深链不选择别的Run。无公开恢复命令，失败/等待只刷新事实。 |
| Campaign | GET /crm/campaigns、/{campaign_id}/enrollments、/crm/sending-identities | 各通道单独loading/error；失败清缓存和不可用发件选项。原POST pause与activate执行暂停/恢复；取消不可恢复。暂停保留在途attempt/今日quota，入站继续。未知POST结果只刷新canonical再作决定，不自动重发。 |
| Settings | GET /settings/research、playbook及versions、country-policies及versions | 读取失败不等于未配置。原proposal命令先持久候选再start Run；网络/503/未知5xx保留不可变body和原Idempotency-Key。恢复先读取历史，再由原键原body返回精确accepted；历史不按内容/最新条认领命令成功。202明确终态；首次严格参数拒绝可编辑。此前未知后再次拒绝不能证明原提交未落库。 |
| Sourcing | GET精确case与uncertain-reconciliations | HTTP键仅验证存在，不是持久幂等保证；域get_or_create_canonical按execution唯一且比对reconciliation_id、actor、reason、artifact。原命令和HTTP键共同冻结。未知后先GET核对精确run/request/execution；相同canonical显示事实已记录、恢复状态仍以Case为准；无canonical且仍可核对时才按原命令恢复。目标切换清草稿，目标消失不自动选择首条；未知命令不静默变目标。无合法恢复能力只提示待核对/不可恢复。 |
| 发件身份 / 入站绑定 | 原身份management/exact、GET /email-inbound/status | start_warmup实际只允许auth_pending且latest auth.all_passed；按钮与方法都禁用非法前置。POST /email-inbound/retry仅传expected_version，不传cursor。waiting未到next_retry_at只等待；固定永久原因保留，history过期重试可能仍blocked，不宣称恢复。409刷新版本，网络未知只读状态后再决定。 |

## 请求隔离

复用useQuoteRequestScope。身份变化、unmount清受限数据、草稿、确认与busy。Campaign对象和Sourcing case变化令旧success/error/finally无效；不同读取通道不能相互覆盖。同身份401/403/404拒绝显式invalidate共享gate，避免在途跨通道响应恢复被拒信息。局部不可见对象只清自身对象，列表权限拒绝清整个受限工作区。

## 验收

先具名RED测试再最小实现；新增web-core-state-recovery.test.ts覆盖错误非空集、Settings 503原body/key、Sourcing原命令与目标隔离、延迟响应、入站版本/期限、认证前置，并复跑对应现有组件。pending/paused/stale/queued/reconciliation_required和预算/配置原因使用真实DTO/受控API，不机械枚举快照。

Browser skill未提供，使用原Playwright。新owner Supervisor隔离PG/MinIO/原四进程，真实页面desktop与390检查关键状态/操作，响应注入只标为受控API响应证据，与原服务真实链路及deferred组件证据区分。只关本owner。边界、显式文件敏感扫描、类型、构建与scope后端回归留实录；本批无API变更则无需生成DTO。源码提交后冻结交controller独立review。

## Controller 裁定增补

实际application在canonical已保存后仍支持原命令继续ack/event，但旧boolean永远false。依ADR0064新增固定recovery_action安全投影，提供已有合法恢复入口，不新增命令。原命令与canonical全字段及actor一致且后端resume_reconciliation时才同键恢复；event_delivered仅说明事件送达。相关原作用组真实Postgres回归必须覆盖各中断点。

HTTP key兼容裁定：新命令从reconciliation_id稳定派生header，首次、同页恢复和刷新后恢复均一致。legacy随机header未持久化且原路由仅校验后丢弃，不要求猜回；仅在后端resume、当前同actor、canonical字段完整时，以同canonical派生稳定header续交付既有核对，不能新建业务事实或换payload。

局部权限边界落实：Sourcing Case 主体/写操作的401/403/404撤销整页generation；已授权Case里的独立plan/quota/candidate/review读取403只关闭相应投影（额度不可读不禁止本来合法的草拟计划），不把局部读取拒绝推断成全部权限撤销。401、身份/Case切换仍全页失效；未知核对写请求的403清原意图及所有旧响应。Settings并行Playbook/versions任一保护性拒绝立即失效，不等待另一请求。
