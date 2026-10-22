# ADR0092：库存公开采集的独立工具授权

状态：2026-10-13，执行用户已批准的公共候选库存设计。

## 决策

库存任务不是 Campaign，也不是旧 demand_discovery WorkflowRun。为当前持有租约的
AcquisitionJob 构建独立 web.search、web.read_page、web.browse_page 和
inventory.search_usage Gateway 实例；不改变原研究工具的 Workflow/Playbook/国家政策门。
本授权只允许查询公开材料并保存公共事实，不能联系人补全、发信、报价或验证需求。
仍必须逐次核对公司、原员工的在职 boss/manager/sales 身份和 job 当前租约。

inventory_discovery 是独立模型能力。模型.generate 原有计量、公司配置、预算、凭证绑定
继续生效；当前员工、原 job 租约和公司模型配置在调用前后重复核查。不能以该能力外推
到研究提案、发送或其他能力。模型只收到公开查询和本次工具取得的材料，不取得密钥。

Codex CLI 默认仍为四轮只读上下文模式。显式注入 DiscoverySession 才开放三个公开采集工具，
最多16轮。每source会话最多7工具请求、140秒、每次100000字节输出；每job最多3来源、
24个已接收外部Gateway请求、480秒。模型输出tokens沿原Gateway预算；失败/未知调用不退款、
不更换key重试。重启或下一次领取后只允许终止缺口，不能重新采集。

每个会话使用同一个串行async task执行工具，保护原task-owned搜索/页面槽；会话取消会
终止当前任务并销毁材料。页面只接受该会话搜索返回的page_id，模型不能提交任意URL。
URL、原件ID、时间和哈希来自受信PageSnapshot；模型名称/国家/邮箱还要通过原文检查。
产品标签仅代表文本匹配，公共邮箱保持未验证。

公共库写入通过save_collected持业务job行锁核验租约，再调用绑定控制租户的目录。
两数据库角色的事务仍独立；不宣称分布式原子提交。外部或控制库写入结果未知后不重新
派发外部请求，已保存的公共事实可供后续独立领取去重。

免费搜索冷启动走inventory.search_usage Gateway。真实usage观察可以初始化上限，不能
抬高已有保守上限，不预留搜索。未知/付费来源不进入计划；真正search仍在Gateway内再次
读取usage并用持久quota预留。API来源状态接口只读取快照，绝不解析凭证或联网。

浏览器在独立browser_worker子进程运行，每次创建空BrowserContext，禁止登录状态、下载、
service worker和WebSocket。Chromium网络走不可达代理，路由逐请求调用既有DNS/peer/robots
安全HTTP transport取得HTML后fulfill；私网/跨源请求关闭。截图、trace和页面写入Artifact
Store。当前不加载外链JS/CSS，因此不承诺复杂动态网站可用；缺Chromium失败关闭，无HTTP
伪装浏览器成功。该范围不增加composition_support例外，不修改九条全局硬边界。

## 运行限制

33项selected来源是能力边界。11类公开网页来源复用搜索→官方企业页面路径，不能称为
33个官方API连接器完成。目录、协会、商会、展会等是寻找企业网站的来源类别，不是可复用
公共企业身份。API型商业源与RSS XML尚未接到本库存collector，保持not_integrated。
国家地址检查覆盖UN M49非洲国家/地区及部分其他市场，别名或无明确地址仍可能不入库。
补库存目标由确定性代码推导：申请交付≤50则目标50，申请51..100则目标100；以同query
新鲜库存数计算缺口。达标后跳过后续普通来源，但仍执行最后保留的探索来源。目标不代表
保证产出，也不改变公司当天交付额度。目录批次标为discovery_only，不允许直接投影；
读取发现材料后才可查询公司官网。官网还须企业名有效词与域名对应，有邮箱时须同域。
品牌域名不对应公司名会漏收，保留为明确的保守限制。

## 审查修复：错误与探索预算

三来源各7次，加首次usage核验1次，最多22次，确保总24次预算下探索仍可真实搜索和读页。
已知页面拒绝/404返回固定page_unavailable，不毒化broker；用完来源预算返回budget_exhausted，
禁止继续外部调用但允许模型整理已有证据。权限、租约、取消及无法区分是否派发的异常仍关闭；
不确定search不恢复或自动重试。开始调用前已超时也只允许最终整理；在途超时保守关闭。
公开工具MCP annotations明确readOnlyHint=true、destructiveHint=false、openWorldHint=true；
这仅说明公开读取语义，CLI approval_policy仍为never，全部Gateway检查保持。
国家证据使用完整实体最长匹配，同地址行出现不同国家时拒绝，避免长名称被短名称后缀误认。
