# Task 11：Run、接管与成本来源观测子规格

## 目标与权限

老板在 Run 全景查看停留步骤、租户时间窗内唯一实体、接管积压和调用输入。沿用 RunAuditService 双层 boss-only 及现有对象页面权限；actor/tenant 来自服务端当前员工，不从客户端统计参数取身份。角色仍是本机演练，不代表多人认证。controller 已裁定无来源的成本输入保持未知，不新增录入账本、工资、钱包、计费或历史回填。

## 来源盘点

| 度量 | canonical 来源、时间与语义 |
| --- | --- |
| Demand Signal | demand_signals.signal_id / observed_at；来源观测时间 |
| Need Hypothesis | need_hypotheses.hypothesis_id / created_at；含后续状态变化 |
| 当前有效 Validated Need | validated_needs.need_id / created_at；仅 validated/sourcing_ready/handed_to_sourcing，排除 fulfilled/withdrawn/lost；状态来自域 NeedStatus |
| Trade Opportunity 记录 | opportunities.opportunity_id / created_at；不等于完整五项资格 |
| Supply Match | 无统一已确认匹配阶段来源，未知；不拿候选或 indicative Product 顶替 |
| Quote 记录 | quotations.quote_id / created_at；内部各状态记录，不代表已批准/发送 |
| Human Execution 接受接管 | handoffs.opportunity_id / accepted_at 去重；仅表示接受，不表示工时/履约 |
| Deal Outcome | opportunities.opportunity_id / closed_at，当前 won/lost |
| 当前接管积压 | handoffs.state=requested、requested_at、assigned_to；不受实体创建窗口过滤 |
| 来源调用 | tool_calls.tool_call_id / created_at、attempt_count；调用数/尝试数与业务实体数分列 |
| Tavily credits | search_quota_reservations 持久状态；consumed/reserved/uncertain 分列；不是费用 |
| 模型 token | StructuredJsonModelClient 仅文本，无 provider usage；上下文 UTF-8 预算不等于 token |
| 人工耗时、费率及金额 | 当前无可信计时/费率来源；等待不是人工工时，cost_class 不是金额 |

各阶段使用各自时间字段，只有 tenant/window 归因，不是同一 cohort 的转化漏斗；不计算转化率。
合格机会缺少逐项可审计五条件联合判定，始终 null 并列可接触客户、真实需求、可供应、可接受利润和执行团队资格证据缺项。Need 当前状态筛选不是重新验证客户证据。

## Typed 契约与最小读取

新增 `GET /runs/observability`，先于 `/{run_id}` 注册。可选成对 start/end（有时区、起点含终点不含、最多31天、不超观测时刻）；未指定由服务端取过去七天。返回窗口、observed_at、scope=tenant_window、completeness=partial、固定缺项、typed 阶段列表、来源调用分组和当前接管摘要。独立于最近50条Run列表及前端筛选。SQL仅select安全聚合，每个来源显式tenant过滤，单语句获得一致快照；COUNT DISTINCT canonical ID，读取/重放不写入任何事实。

接管摘要含总深度、按当前 assigned_to 的安全ID/未分配数量、最久等待及时间异常数。无队列时等待null（不适用）；任一requested_at晚于观测时刻则最长等待未知。不用abs或clamp修饰异常。当前队列服务只支持requested，无虚构escalated状态。接管页仅展示当前授权列表数量并说明最多50条，不冒充租户全量。

RunDetail新增安全观测摘要：实际tool_calls唯一数/尝试数，token/人工秒数/总费用/单位合格机会成本未知，Money/Decimal契约保留。所有时序一致才可给记录时钟经过秒数，明确不是模型或人工工时；反序时间输出未知。对象引用仅经持久化同租户关系核验：human_handoff subject_ref→Handoff→Opportunity→Need；sourcing_case→Case→Need/Opportunity。不依据任意字符串前缀猜链。审批原有精确链接保持。非接管Run无可信负责人时显示未知。

## UI及安全

Run页独立观测区显示范围/窗口/来源/完整性/缺项，刷新与原scope generation/AbortSignal一致；读取失败清空该区，身份失效清空全部受保护观测。停留步骤、处理责任和精确对象入口在详情展示。错误仅固定分类及安全引用，无context、subject/body、地址、locator、hash或凭证。日志和通知出口不新增载荷。受控环境中的持久记录不代表真实获客成绩。

## 验证与交付

真实PG测试去重、重放、窗口边界、跨租户、无费率不形成金额、未知不0化、异常时序及安全绑定。API当前身份和服务授权测试；UI测试展示/错误/迟到响应及对象导航。复杂场景使用真实PG/Gateway/域，外部端口合成；Browser plugin不可用，原Python Playwright在1440和390真实有内容页面验证身份、非空、遮罩、console、交互与截图并实看。

源码与固定task-11-report.md分开提交，聚焦门禁、OpenAPI独立生成、显式路径敏感扫描、结构自检。精确清理本owner；保留其他owner/控制器ledger，不推送、合并、部署或真实发送。

## 实际口径补充与裁定

所有状态和attempt_count均为observed_at读取时的当前值：窗口内创建工具记录的当前累计尝试、窗口内创建预留记录的当前consumed/reserved/uncertain状态，不声称这些尝试或消耗都发生在所选历史窗口。Need/Quote/Outcome也不是历史as-of快照。没有事件时间证据，不重建期间发生量。

OpportunityList没有精确机会deep-link接口。controller接受只显示核验后的Opportunity ID；从Run的持久化绑定导航现有精确Handoff（含机会组合）和Need路由，目标API重新授权。没有绑定就明确不可定位，不取租户任意接管替代。
