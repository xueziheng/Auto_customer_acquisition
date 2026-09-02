# apps/api/ —— HTTP API（Phase 1 深）

## 职责

服务 Web 前端的 FastAPI 进程。router 按 16 个产品页面分组（设计稿第四十二节），每个 router 只做四件事：解析请求 → 判权装饰 → 调域服务 → 返回域 DTO。

**不在 router 里写业务规则、不在 router 里定义业务结构**（请求/响应模型引用域的 `schemas.py`，只在需要时裁剪字段）。

## Router 与页面对应

| router 文件 | 页面 | Phase |
|---|---|---|
| `command_center.py` | Agent Command Center（老板/员工自然语言入口） | 1 |
| `demand_radar.py` | Demand Radar（信号/假设/需求簇） | 1 |
| `customer_discovery.py` | Customer Discovery（潜在企业与评分） | 1 |
| `campaigns.py` | Campaign Center | 1 |
| `inbox.py` | Smart Inbox | 1 |
| `crm.py` | CRM & Opportunities | 1 |
| `products.py` | Product & Supply Center | 1（只读为主） |
| `sourcing.py` | Sourcing Center（Phase 1 人工操作界面） | 1 |
| `costing_quotes.py` | Deal Cost & Quote | 1（人工录入） |
| `team.py` | Team & Territory | 1 |
| `work_uploads.py` | Work Uploads | 1 |
| `commitments.py` | Commitment Center | 1 |
| `approvals.py` | Approval Center | 1 |
| `runs.py` | Run Center | 1 |
| `settings.py` | Playbook & Connections | 1 |
| `billing.py` | Credits & Billing | 3（不建） |

## 三条纪律

1. **判权装饰器 + 服务层判权双保险。** router 上的装饰器是第一道，域服务内是第二道——worker 不走 router，只有服务层判权才能覆盖它。
2. **产品客户视图只经 `get_customer_view`。** 对外暴露的产品接口不允许调内部视图方法（成本泄漏是永久损伤）。
3. **响应里的每个关键字段带 provenance 摘要**——前端「为什么判断高意向」的展开数据从这里来。

## 入口

`main.py`：FastAPI 实例、依赖注入装配、中间件（租户上下文、审计、错误转换——把域错误映射为结构化 HTTP 响应，`is_retryable` 转 Retry-After）。

## Phase 2 安全报价 HTTP

`quotation_actions`与旧成本router共用前缀但不改旧CRUD/readiness。只接受域公开命令与
安全投影；来源原文仅在独立授权preview/locator返回。C、U、F用途不能互相替代，
身份仅来自现RequestIdentity，域仍重读当前员工。
确认路径必填原Idempotency-Key并在OpenAPI声明；calculate/preview/locator/submit与文件
server-canonical/NONE操作不把客户端key当身份。空命令拒绝null和额外控制字段，
文件路径拒绝未知query/body，PDF只返回后置授权成功的bytes与canonical文件名。
本组错误局部映射，未知依赖固定503；全局400/401不改。只有真实Gateway结果保留
QuoteFileApiError技术ID，Retry-After只取其合法显式值，不能用全局默认时长。

报价runtime仅接受显式TRADEOS_QUOTATION_SETTINGS_JSON，core/evidence整体装配，files
独立整组启用；缺组固定不可用，不借手工发送Gateway。source/domain工厂必填运行tenant，
唯一approvals与七个handlers必须在唯一engine构造前形成；延迟issuer/run闭包只发布一次。
lifespan先schema、同parser受信probe、再yield；退出/取消/before-yield故障先aclose再dispose，
保留primary。普通parser能力降级仅关闭真实parse，不禁用metadata、审批或独立PDF。
API报价结果仅经原structured_log通知出口，不宣称站内已投递，也不启动expiry后台任务。

## Sourcing V2 HTTP

`/sourcing-cases` 只提供 Case/ladder/candidate/public-plan/quota/review/reconciliation 的安全投影和 plan/confirm/run/review/reconcile 命令。所有命令要求相应角色与精确 ID；confirm/run/review/reconcile 还要求原始 `Idempotency-Key`。API 不构造搜索、页面或模型依赖。`lane` 未持久化时必须为 null/unknown，页面公开事实不得显示为 verified contact 或 quoted price；5xx/无权限必须保留局部错误状态，不能伪装为空列表。

## Need Cluster 寻源准入 HTTP

老板通过 `/commands/sourcing-admission-proposals` 提交并确认完整、显式的
`cluster_ranked` 配置；确认只激活 Directive，绝不直接启动 Workflow。准入列表与详情仅返回
寻源域安全投影，并由 application 层附加 `policy_not_configured`、
`automatic_admission_disabled`、`policy_status_unknown` 或 `enabled`，不得把 scheduler 状态写回域。
人工准入仅允许 boss/sourcing，必须验证恰好一个未经修剪的原始 `Idempotency-Key`，并经
`workflows/sourcing_case/application.py` 与 scheduler 共用的单项 claim/start/bind 路径；错 tenant
和错角色须在服务 IO 前拒绝，内部依赖错误统一映射为脱敏 503。
