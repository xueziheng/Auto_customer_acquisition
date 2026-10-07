# 全分支最终审查上下文（Task0–13已独立完成；精确HEAD由委派给出）

此文件是审查交接草案，不是通过结论。分支起点为包含本计划的ec801a8（功能基线1b760b2），工作树为.worktrees/web-core-completion，分支codex/web-core-completion。controller在派发前提供完整BASE/HEAD和review-package；任务0–13已独立Approved，controller提供最终审查HEAD。本次审查不能被之前每批review替代，也不要求重做全部历史测试。

## 实际交付边界

用户授权本机受控Web优先、桌面接口契约保留。本轮无Tauri、真实客户发送、供应商联系、共享服务器认证/部署、push或merge。开发身份选择不是真实多人登录。正式能力矩阵必须把受控主链、研究链与尚未运行/禁用能力分别标明。Cost/Qualified Opportunity缺可靠输入时保留未知，不将合成记录数当真实业务成果。

Mac原ControlledConfig缺quotation运行配置，证据解析器要求Linux。完整报价验收是独立internal-network Linux owner公开回复产生其自己的Need/Opportunity，然后走真实来源/单位/成本/独立审批/PDF；不是Mac Gmail同一实体，也不证明Mac统一入口能报价。Task12需最终同版本分别验收；Task13需确切平台/启动或验收命令。不能以临时QA配置桥接冒称统一入口已经交付。

## 全分支审查重点

Task12实际端到端又发现并补接的范围须重点纳入最终diff：旧controlled入口缺research_only执行装配；原prepare_sent直接record_verification不等于单Provider整链。最终应复用ResearchRuntimePorts及ADR0065 typed contacts_factory，在同runtime core/outreach与PG ledger上构造原Gateway插件，验证Provider→VerifyContactsStep→域→已批Campaign发送。真实Hunter readiness不能伪造，研究阶段仍不得创建联系人/Campaign/send。状态/证据以Task12最终report为准，不能用旧Task4生成提案的成功代替执行成功。

恢复证据界限：owned Docker随机HostPort在PG单独stop/start后改变，原配置不会跟随，不能判API池故障或声称透明DB容器重启。A5改同owner pause/unpause维持原端点，并需独立解暂停计时器，避免wait_for取消也等待暂停数据库；实际超时/重叠误启及主动中断历史需报告。此测试不能把查询1秒超时写成总耗时1秒，最终必须await恢复/清理，没有paused容器或后台查询残留。HUP只重启应用的契约不扩大。

- 按正式设计A1–A10和最终验收表核实际入口、持久事实、UI、失败恢复、证据关联与未验收边界。Task1/2通用Agent部件验证不等于生产任务消费者已启用；Catalog培养仍queued，Browser Worker无任务来源仍disabled。
- 依赖方向apps→workflows/runtime→domains→shared；进程不互导，域间不直接导内部实现，外部业务动作唯一Gateway。凭证不得到模型上下文、日志、浏览器响应；不读真实.env/连接/密钥来审查。
- 入站正文/Raw的大小深度预算、有限charset、引用历史隔离、完整正文敏感guard、当前连续span及真实Provenance。下一问只是建议，不能写已存/已发；未验证可达性不能进发送序列。
- 当前tenant/employee角色与归属约束应跨读写、原件下载、纠正、下一问、通知/接管保持；SQL先过滤再LIMIT、写锁顺序和最后资源核验不可被旧fixture或UI适配绕开。
- 原审批、报价、金额Decimal、quoted与indicative人工风险接受语义不变。报价批准/PDF下载不等于发送、成交或需求fulfilled；未批准不能通过前端导航变为已批准。
- 新入口与恢复逻辑保留原业务幂等权威：Settings冻结精确body/key，Sourcing依据canonical reconciliation而非丢弃的历史header；未知中间状态和响应丢失区别。成本选择B在非授权读取失败后不能回退URL A；401/403和scope改变仍清数据/确认。
- Run观测canonical实体去重、窗口与当前状态、当前队列独立、绑定精确且重授权；等待/记录跨度不是人工工时，反序时钟不能变正数；缺usage/费率/资格不补零或概率。
- 生命周期启动/失败/重启/停止必须精确owner/PID出生/容器归属，private文件清理含reply-model.sqlite系列；不得泛kill/prune或抹去历史cleanup_unknown。未知发送测试计实际Provider调用，不能依赖合成邮箱去重给假绿。

## 需按最终结果逐项核的保留事项

controller派发时以progress.md的全部`Ruling:`、`minor (deferred)`和`parked`条目以及正式裁定记录为准，不能静默丢弃。当前具名清单：

另有Task2复审Minor已显式裁定暂不加适配器内部await取消测试（ledger第79行附近）：新链只捕获Exception、CancelledError传播，原Worker取消/资源清理已有覆盖，未发现实现缺陷；代价是新适配器挂起点的直接覆盖有限，生产provider接入或异常分类变更时补。最终review须看见该保留项，但不预判其分级。Task3b/6的Git AppleDouble环境噪声、Task4历史VueRouter R0004提示和Task8 lint归属也不能从裁定记录消失。Task5b binary schema Minor已由Task8解决，保留解决记录，勿重新当未修项。

1. Task6 HTML void集合误压栈，Task12应最小修复并验证当前字段不跨引用拼接。
2. Task7纠正接口跨租户PermissionDenied的旧docstring，Task13应只改说明。
3. Task9测试标题过度声称延迟覆盖，Task12应收窄或写真实断言。
4. Task11 sourcing成功/版本不匹配/Opportunity.need_id不匹配三项绑定覆盖，Task12应给聚焦证据。
5. 四个scanner旧合成marker fixture、全仓lint实际warning归属、Task11发现的reply-model.sqlite清理缺口，Task12不得绕过门禁。
6. Settings内部candidate commit→Run start故障，不等于Task9已测服务器202后丢响应；按Task12最终证据核中间恢复。

## 审查方式

读取适用AGENTS、正式设计/计划、最终报告与能力矩阵，以及提供完整diff；按块顺序审查，不重复生成diff或无目的扫描。每个具名具体风险可定向核必要未改契约，写明风险/文件/结果。既有已报告同版本测试不重跑，新具体疑点才最小验证。共享.git AppleDouble噪声只保留事实，不维修共享仓库。

只读实现/index/HEAD，不派子代理；仅写最终审查报告。逐项分级、file:line、真实后果和建议，区分已发现缺陷与Cannot verify。controller只安排一次统一修复波及一次限定复审，残留需显式裁定；本文件不预判任何发现的级别或结论。


Task12最终交接：报告5b63744、controller验收aa24508；48e4465完整9318passed/0failed/0skip/0warnings，1630冻结源码hash匹配。独立审查发现A3原fixture错记Approval提案人，I1修复源码7e10383仅2测试文件，真实提交人自批400后pending、第二boss HTTP批准且精确对象绑定，新owner8d234c0956fb498e9f171342c3b72f51主链1passed/34.97s；同review_task12双Approved。全量与纯测试修复分栏，不第三次无依据full已裁定并获review认可。先前独立审批假绿历史已撤回。最终Linuxowner2ea6b4a4e32f429192c2d1926a2ebfc1 code0/cleantrue，root/reviewer均看代表4图。早期3/2warnings类别缺失仍未知；112Web lint有基线归属。详情见Task12正式验收与审查报告，不重跑既有同版本测试。


Task13最终交接：初始SOURCE0bfb4d1、报告fe6f7dc；review发现恢复测试close异常跳过后续释放、证据写失败未恢复logging及表格空行，fix SOURCE041bc741/reportfebf7e0在同reviewer round1全部关闭、双Approved。4项无DB故障覆盖真实RED→GREEN，正常新owned恢复1passed9.67s；只新增测试/文档，生产仅两docstring AST不变。备份仅静止owned PG+一份原件→另一新空owned目标，hash一致/source不变，非PITR/运营中备份/自动恢复launcher。正式docs/acceptance/web-core-delivery保存32历史快照、87条Ruling所在行的旧截止快照与manifest，source/archive精确hash已验，后续controller裁定会另追加完整最终ledger，旧快照不改写。六张Catalog测试副产物已保存hash且原六路径恢复BASE。正式task-13-review已由controller精确复制，最终全分支review仍待。
