# ADR 0017：研究发现的工作流版本与来源归属

状态：已接受（Phase 2 首批已批准范围内）

## 决策

保留 demand_discovery v1 定义、handler ref 与历史行为；另注册 v2，其 handler ref
使用 demand_discovery.v2 前缀。引擎不改动，start 仍选最高版本。v2 每步读取已确认
提案的 execution_mode；缺省 outreach_preparation，所以历史提案即使新建 v2 Run
仍沿原触达准备语义执行。research_only 不要求 Campaign/role/assessment，且最终
handler 不持有联系人或报价端口。旧排队handler也显式拒绝研究模式。scheduler
允许没有 account_discovery 的研究组合；无联系人组合时旧触达排队失败关闭。

研究仅在组合明确配置 Tavily 免费能力时运行。默认 Brave 组合不被研究借用，返回
unsupported，零搜索调用。免费额度错误保留 quota_exhausted、usage_unknown、
paid_enabled、request_uncertain、unsupported，绝不回退付费服务或重试不确定调用。
已有搜索额度仓储与HMAC重放保护不改变。
Run的searches_used/pages_used沿预算尝试计数语义，含被拒绝尝试，不得标成Provider
已计费credits。实际免费用量/不确定性来自持久quota snapshot/run_state。

## 证据与模型边界

查询必须显式带 importer/distributor/ecommerce 线路，目标国家、品类、排除项与四个
预算来自已确认提案。ResearchEvidence 由受信编排从确认查询及原页计算；模型只见
脱敏页面正文，不能设置来源类型、企业身份、官网、线路、核验状态。URL/时间/hash/
不可变artifact仍由页面读取器产生。跨线路同页保留独立Signal，discovery_key 由
提案、query、lane、查询国家/品类、URL确定性产生；相同提案重放不会重复写信号。
原Signal唯一键增加discovery_key，旧记录为空键、research_evidence为空，不回填旧身份。

生产TradeManager的新模型输出在两种模式都强制每条query的非空lane；缺失/空值
直接拒绝，不补猜。持久旧payload decoder及内部旧dataclass仍允许lane为空，默认
旧outreach语义；它们用于旧提案/旧集成调用兼容，不是新模型输出的准入路径。

目录描述与公司自述均是有归属的公开观察，不是运输、购买、OEM事实或客户回复。
公开RFQ在研究模式下也不提升到客户回复证据等级。待核验Signal既在workflow也在
demand服务中禁止创建Hypothesis。官网证据充分的结果调用原resolve_account，
原active account/category合并保留全部signal_ids；重复同页不被算成独立置信证据。

## 当前识别能力及代价

自动核对故意保守：支持英文句首的 “We are <企业名称>, a/an/the ... .” 以及同一
主体 “We are / <企业名称> is headquartered/based/located in <国家>”；
名称目前仅英文ASCII字母、数字及少量公司名称符号。国家支持 US/DE/GB/CA/AU/NZ/
FR/ES/IT/NL 及其英文全名（美国含United States of America）。其他语言、句式、
国家名称即使目标可配置，也只保留pending_verification。配送地、分支机构、TLD、
Contact us 和搜索国家都不构成所在地证据。

出现目录/名录/经销商定位器标记或目录路径即保守待核验，不把目录host当买家官网。
普通官网正文不必出现域名；host与明确本企业身份/所在地自述共同作为证据。
这不是工商真实性核验，也不能保证网页自述无虚假；宁可漏收，后续由真人核实。
未知来源/模型未提取有效信号与无搜索结果分开记录，不声称已发现合格贸易机会。

## 页面安全与授权

授权仍是已激活国家政策、Playbook、用户权限及Gateway批准搜索结果，不增加逐host
静态许可白名单。网页仅从一次性搜索结果句柄读取。正文与robots请求逐跳校验URL、
DNS与实际peer IP；重定向只允许原始scheme/netloc，不自动跨域或升级跨来源。受信
部署可提供deny hosts。robots大小上限512KiB、每跳超时及跳数有界；404/410仅表示
未提供规则。未知状态、robots读取失败/畸形/额外未实现速率限制均关闭。

robots分组按TradeOS-Agent/通配符、最长路径及Allow同长优先匹配；支持*和$。
这是有界实现，不宣称完整爬虫规范/全网条款审查。参考
[RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html)：robots不是访问授权，
Allow不等于许可证，也不能推翻Gateway国家政策。

识别登录/验证码/禁止访问的拦截标题与明确禁自动抓取文案，并在快照写入前阻断；
密码表单且表单外公开可见文字不足12词且不足80字符，也视为登录墙（排除head、
title、脚本样式、导航和页脚）。这是保守启发式，不是通用网页理解或登录墙完备检测。
普通公开页登录导航、联系表单验证码组件不单独构成登录墙。未覆盖所有挑战实现，
不尝试解验证码、带Cookie、绕过反爬或登录。不读取真实Provider凭证执行验收。

## 迁移与兼容

0040追加demand_signals.research_evidence JSONB与discovery_key，并禁止修改这两列。
不新建Lead对象，不修改旧Signal或旧提案内容。回滚到0039若已有跨线路同页数据会
与旧唯一键冲突并失败关闭，需要人工导出和制定数据迁移方案；绝不自动删证据。
常规空库upgrade→downgrade→upgrade及当前ORM契约通过独立迁移测试验证。
