# Task 10 正式子规格：精确对象的跨页面连接

基线：eec91eaf4c4356c3ff7a8b24f137056e3de5c28a。仅修现有 Web 连接与其请求作用域，0–9 已验收功能继续复用。

## 真实接口盘点

- OpportunityDetail 的 `OpportunityView` 已返回 `opportunity_id`、`need_id`；详情仅展示需求文本且成本页面初始机会输入为空。新增需求详情链接与 `/costing-quotes?opportunity_id=…` 入口。
- 成本页面仍用原 GET `/costing-quotes/opportunities/{opportunity_id}/cost-sheets`、`quote-context`、`price-evidence` 及 QuoteVersions 的报价列表。新增消费单一 `opportunity_id` query；可选 `cost_sheet_id` 只精确选择已授权列表中的同机会成本表，缺失或不匹配不得选择首项。无成本指定时保留原列表选择行为。
- 已有 `/costing-quotes/quotes/:quoteId` 通过 GET `/costing-quotes/quotes/{quote_id}` 读取指定报价，返回 `QuoteInternalPublicView.opportunity_id/cost_sheet_id`。显示链接到这两个精确对象的成本工作台。报价详情入口保留独立文件授权，不能因为无内部成本读取权关闭当前合法文件入口。
- POST `/costing-quotes/quotes/{quote_id}/submit` 返回 `QuoteApprovalStartResult.quote_id/run_id`，仅表示工作流启动。新增本次真实返回 Run 的 `/runs?run=…` 链接，仍明确尚未批准和发送。路由或身份变化清除引用。
- `RunDetailView.approvals` 含 `RunApprovalView.approval_id`，新增 `/approvals?approval_id=…` 链接，沿用现有精确审批读取和决策流程。
- SourcingCaseDetail→Need、ProductSupplyCenter→Case、Catalog→Approval 已存在，保留准入、canonical恢复、预算和 queued/stale 文案；不重造链接。ApprovalView 无具名 quote_id/业务资源路由，仅 affected_entities 和展示字典。控制器裁定不猜反向链接，属于当前接口限制，不新增 API。

## 作用域与失败语义

query只是输入，不是授权或事实。重复/空/无效机会query显示明确错误，不启动业务读取；孤立成本query不得绑定到人工输入的另一个机会。路由输入变化同步清空旧请求、成本/报价/计算/证据/确认及草稿输入。沿用 `useQuoteRequestScope`，新增query必须纳入所有既有页面/写入/幂等确认scope，不新增第二套门。

GET仅应用当前scope结果；成本列表与准备事实须核对返回的机会ID，指定成本只选择精确匹配。403/401拒绝时清除该组合读取的受限状态并使并行旧响应失效；网络/404错误不得展示上一个机会资料。不因跳转自动执行来源确认、单位确认、计算、报价或审批。来源、scope/evidence/hash/有效期、数量/单位、冻结与当前PDF授权仍由原域裁决。

## 验证与交付

先新增有意义失败测试：Opportunity链接、直开/同组件query切换/重复query/精确成本缺失、旧响应与身份切换、Quote→成本/Run、Run→Approval；再实现。聚焦保留原 quotation-flow、costing、opportunity、Run、sourcing/product/catalog 相关回归。

实际浏览器流程：新建owned受控环境，原公开发送前置及独立审批，经合成入站→真实分类/Need/Opportunity；从机会点击成本并刷新核对对象，再检查Need/寻源入口及原审批链。后续成本/报价引用只经原证据确认端口，合成供应证据明确fixture。若原runtime能力具名不可用及时向控制器报告。1440×1000与390×844实看长ID、证据及主要操作，截图与受控响应浏览器覆盖分开。

必做聚焦测试、类型/必要lint/build、结构检查、显式改动路径敏感扫描与diff-check。无API变更不重新生成类型。源码本地提交冻结、报告独立提交；报告记录RED→GREEN命令/exit/准确计数、版本、实际边界、资源清理和concerns。全仓A1–A10由Task12完成。

## 实际发现与控制器裁定

- 基线 OpportunityDetail 仍把有 Provenance 的关键字段和金额显示为“已验证事实”；Task8 对 Handoff 的修复不能等同此组件已修。经真实截图和 RED 证实，窄修为“关键字段 / 来源记录”，保留原来源类型、确认人与时间展示。
- CRM 全局 body min-width 1080px 导致390px的机会链接和后续成本页裁切。按控制器裁定移除该全局下限，仅为当前机会看板增加单列与滚动布局；成本长ID挤窄标题改为mobile标题与ID分行，不改全站框架。
- 本机原 ControlledConfig 未装配 quotation 技术配置，Mac 原 Linux parser 平台能力也不可冒充。控制器接受两套清晰分栏证据：本owner真实入站→Need/Opportunity→新增入口与成本503；原独立 internal-network Linux owner的完整公开回复→Need/Opportunity→原来源确认/单位/成本/独立审批/PDF。不是同Need、同owner或同一Mac入口。报价主入口仍有平台/配置限制，最终必须 DONE_WITH_CONCERNS，Task12/13保持能力矩阵。
- 原Linux准备失败时原server只报告异常类，且stack把安全日志放进最后被丢弃的异常消息。经控制器授权，只补仓库 tests 相对文件/行号与固定module名的安全frame定位，并在stack现有safe_output过滤后输出，不输出异常正文、locals、配置、请求体或原始日志。预算、parser探针及网络隔离不变。
- 旧 quotation=True tar 只列部分 composition_support 文件，新增装配已依赖的五个文件需精确加入白名单；A-only 不变。旧 HistoricalEligibility 无 actor 扫描 list_inbox 已不符合当前公开契约，仅测试适配器委托现成 CurrentReplyStatusReader，以固定 NOW 保留 tenant/contact/account 和 unknown 拒绝。
- Run 真实当前权限是 boss-only。员工点击报价返回 Run 链接保持403，具备权限的老板读取同一精确 Run 后点击其具名 approval_id；不为跨页放开审计权限。原 Linux 完整 E2E 延用360秒执行/45秒清理预算。
