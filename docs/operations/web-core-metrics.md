# Web 核心观测：口径与排障

老板打开 Run 全景，先核对当前租户与页面列出的起止时间，再看阶段记录、当前接管队列、单个 Run 的停留步骤和精确对象绑定。Run 列表只有最近50条，列表筛选不改变独立租户观测。当前界面为本机受控演练，不以这些记录证明真实获客成绩。

## 范围和来源

`GET /runs/observability` 默认读取服务端过去七天，也可显式提供成对、有时区的 `start` / `end`，最大31天且终点不晚于当前时间。起点包含、终点不包含。响应的 `observed_at` 是本次读取时间；SQL单语句保证这些聚合在同一PG快照，所有源限制同租户。当前boss-only身份与服务二次授权均保留。

阶段是各自独立时间窗口中的canonical实体数，不是同一批需求的转化漏斗。不能把研究Run摘要相加代替唯一实体数，也不能用重试、Outbox投递或接管事件代替实体。

| 显示项 | 主键与窗口字段 | 当前状态限制 |
| --- | --- | --- |
| 需求信号 | signal_id / observed_at | 全部 |
| 需求假设 | hypothesis_id / created_at | 全部 |
| 当前有效已验证需求 | need_id / created_at | validated / sourcing_ready / handed_to_sourcing；排除fulfilled / withdrawn / lost |
| 贸易机会记录 | opportunity_id / created_at | 全部；不能当合格机会数 |
| 供应匹配 | 暂无统一已确认来源 | 未知 |
| 报价记录 | quote_id / created_at | 全部；不是已批准或发送数 |
| 已接受接管的机会 | opportunity_id / accepted_at | 按机会去重，不等于人工工时 |
| 成交结果 | opportunity_id / closed_at | 当前won / lost |

状态均指本次观测时的当前状态，不是历史as-of状态；已验证需求也不是累计曾验证数量。资格缺少可接触客户、真实需求、可供应、可接受利润和执行团队的完整联合证据，因此合格机会数与单位合格机会成本未知。

## 成本输入

工具使用 `tool_calls.created_at` 选择记录，显示非duplicate调用数、这些记录当前累计attempt_count、独立duplicate重放回执数。计数包括被拒绝记录，不能直接称为实际外部请求数，更不能用cost_class推算费用。重放回执不重复计入调用或尝试。

Tavily仅按 `search_quota_reservations.created_at` 选择本地预留记录，再按当前consumed / reserved / uncertain分列。它们不是账户总用量，也不证明消耗发生在所选窗口；遇不确定状态必须保留原Run/原键核对，禁止清表或自动补零。

模型客户端当前只返回文本，没有可信provider usage；上下文UTF-8预算估算不是实际token。没有人工工作计时、可信费率及完整资格判定，所以费用总额和单位成本保持null并显示缺项。不存在录入端口，也不由模型补数字或改历史事实。未来如提供计量，须先规格化来源、权限、幂等和Money/Decimal，不能拿本页只读投影当计费账本。

## 接管与Run排障

当前租户接管深度只统计 `handoffs.state=requested`，不受上面阶段窗口限制。按assigned_to列安全员工ID，无人分配时提示经理处理。requested_at晚于本次观测时间则最久等待未知并列时间异常；空队列等待不适用。等待秒数与接受接管都不是人工实际工作耗时。员工接管页只展示当前授权列表的可见数量（最多50），不能当全租户深度。

Run详情中的“记录时间跨度”来自Run创建与步骤最后更新时间，不是工作时长。步骤更新早于创建、步骤/工具早于Run创建、工具结束早于开始都会标为时间异常并保留未知；不计算负数、不取绝对值。固定合成时钟与真实时钟混合的测试记录不能用于耗时/绩效。

human_handoff通过持久化同租户Handoff→Opportunity→Need核验，sourcing_case通过同租户Case→Need及已绑定Opportunity核验。不从自由文本subject_ref前缀猜链。机会工作台没有精确机会deep-link，故只显示核验后的机会ID；导航使用原精确接管（包含机会组合）或Need路由，并由目标API重新授权。其他Run没有可靠对象绑定时明确不可定位；审批仍使用后端approval_id。

遇401/403清空所有已读观测和Run数据；其他观测读取失败清空该区并显示固定分类。不要把失败显示成零记录。页面、日志与通知不增加正文、地址、原始context、locator、hash或秘密。
