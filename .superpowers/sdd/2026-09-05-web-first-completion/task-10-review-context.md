# Task10 独立审查上下文

本文件是需求上下文，不是审查结论。具体BASE/HEAD及diff由controller在Task10完成后提供。读取task-10-brief.md、该批正式规格与task-10-report.md，核精确对象ID、路由刷新/身份切换的请求作用域，以及保留原审批、报价证据和金额边界。拟议文件映射按实际行为核验。

浏览器演示从真实受控回复生成的Need/Opportunity起步；原公开前置端口与合成外部系统必须区分，禁止seed下游结果来证明链路。跨页面链接不是授权，也不证明供应价quoted或报价已批准。原单位/数量、scope/evidence/hash/有效期和PDF当前授权仍生效。Catalog queued/stale不得显示培养完成。

审查只读，不派子代理；完整diff一次或顺序chunks，截断hunk才补文件，域外每个具名具体风险只做一次定向检查并报告风险、文件和结果，不设导致必要前提猜测的总计一次限制，仍禁止泛查。不要重复报告中同版本测试；具体未覆盖风险才定向验证。结论含Spec与Task quality、具名分级问题/行号及Cannot verify。

## Global Constraints

- 根 `AGENTS.md` 的九条硬边界和逐次人工审批要求保持不变；进入实现目录前读所有生效的就近规则。
- 本轮先交付本机受控 Web；不实现 Tauri，不开展真实客户发送、供应商联系、采购或部署。
- 金额只用 Decimal；模型不输出最终金额和概率；事实、推断及客户表达结构分离。
- tenant、actor、scope、Provenance、审批结论、预算不能来自客户端自报或测试绕过。
- 保持 `apps → workflows / agent_runtime → domains → shared`；进程之间不得互导。
- 新外部工具使用 manifest + check + handler 插件；不往 Gateway 核心增加具体业务分支。
- 已有 Catalog 培养 Case 仍止于 queued；本轮不新增培养消费者或默认生产策略。
- 每批次保留有意义的失败测试与聚焦通过证据；全量测试在集成里程碑执行，未改代码且无新疑点时不重复跑。
- 所有新文档用中文；API 类型从 OpenAPI 生成，前端不手写重复业务 DTO。
- 本计划中的“新增路径/接口”为拟议实现，不表示文件或能力已经存在。未来批次先产出该批详细规格，再按测试推进，不凭这份总计划猜测跨域契约。
