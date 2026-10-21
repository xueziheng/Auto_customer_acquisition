# ADR 0115：客户自动准备范围纳入 Campaign 精确版本

日期：2026-10-22。状态：接受（执行接线仍需单独验收）。

## 背景

搜索发起者不等于客户负责人，开发项目的批准也不等于企业批准具体客户开发。现有 Campaign 边界没有自动准备的负责人或联系方式处理依据范围。

## 决策

在原不可变 CampaignBoundary 中增加可空 customer_agent_policy；空值表示关闭，旧版本不迁移开启。非空政策最初只支持企业法人、职务邮箱、真人明确提供的 legitimate_interest 评估引用与单一负责人。确认身份与时间由当前服务端认证 Actor 和时钟生成，客户端不能提交。这只是适用范围确认，不声称系统完成法律评估或确认具体网页事实。

原 Campaign 创建、修订、提交和精确版本审批机制覆盖该政策。审批包仅在政策非空时增加字段，保留旧空政策的 canonical 审批内容。修改政策产生新版本并清空审批；原版本保持不变。版本存储增加可空 JSONB 列，不新增跨企业共享记录。

后续自动执行必须在搜索请求中明确引用已批准精确版本，持久保存授权关联，在每一步重读当前员工、负责人、知识引用、国家政策和 Campaign 版本。政策本身不授予发送能力，不改变全局默认关闭开关，也不替代来源证据、处理依据、可达性验证、抑制与当前发送 preflight。

## 验收边界

本 ADR 的政策契约与版本持久化可以单独测试；只有搜索 API 到实际验证及入组的完整合成链路通过，才可宣称自动客户准备完成。数据库迁移不触碰任何原密钥、来源或运行配置。

## 确认产品上下文补充（2026-10-22）

政策增加明确 document_id/revision_id 引用（最多5份）。旧政策缺引用可读回，但自动消费必须阻断并要求补资料、产生新版本重新批准。不得凭 allowed_categories 标签推断已确认产品，也不得自动选择企业全部目录。

scope 引用与 query 引用分别复核，均经 ConfirmedContextReader。scope 引用只参与自动准备内部适配，不改变 InventoryQuery 的公共库存选择：即时库存可交付并入队，但建联系人和入组前仍须当前资料复核与有据适配。query.confirmed_refs 非空继续走原 private fit。确认资料不表示正式产品发布或客户可见许可；邮件内容仍走原客户视图与审批。

## 跨进程复用与来源边界

取得与归属的机械同事务桥接归属 infra/db/customer_assignment.py，API只保留兼容导出，scheduler使用同一实现。跨域授权编排归属 workflows/customer_preparation，经显式注入的员工、Outreach、审批与确认资料公共接口读取当前事实，不扩大composition_support白名单或引入反向进程依赖。

Agent资料独立使用fill_account_profile_from_evidence：必须匹配当前租户原件、实际Gateway页URL、重新计算的原件hash与逐字引文，并核对客户官网；只补空字段、冲突拒绝、不写人工确认。模型适配仍是推断，不能因此制造ValidatedNeed。原人工入口保持原语义。搜索/分配保留ADR0093的在职老板权限，负责人可委托在职sales/manager。

## 实际执行与发送前复核

准备项与 InventoryRequest、分配在同一租户事务写入；即时公共库存与异步 private fit 共用同一入队点。原 singleton 库存阶段先处理一项准备，再执行原采集，不新增后台循环。租约 CAS、固定幂等键、真实 ownership_locked_at 与阶段检查点共同控制恢复；外部步骤已派发而结果未知只保留核对状态，重建进程也不能重派。

消费者复用 canonical Prospecting、Demand、Outreach 和原联系人 Gateway。企业资料只作内部适配，网页事实、适配推断和真人范围确认分开保存；实际验证通过后才按原归属快照与 source_hypothesis 入组。缺真实 sender、验证配置、有界原件、处理依据或当前政策都显示固定阻断原因。全局发送关闭允许准备及入组，不允许发送。

国家政策读取使用现 CountryPolicyDecisionReader 约定的具名 SYSTEM 只读事实身份；业务请求仍由当前真实老板、owner、精确 Campaign 审批和确认资料检查授权，SYSTEM 不替代业务命令权限。新授权路径在 API 与 scheduler 的原 email.send Gateway permission 阶段追加具名复核，不改变原六阶段次序；旧无准备绑定路径保留原检查。当前仅支持 retain.assessment_ref、honor_opt_out，未知 requirement 拒绝。

scheduler 读取与 API 相同的 Hunter 元数据文件与惰性凭证解析器；只有当前配置与已验证 readiness 相符、双工具实际注册且持有原 singleton 时才提交 runtime_composed，不代跑验证探针。customer_preparation 模型能力沿现平台目录、员工/企业绑定、配置版本和 export 授权解析；不添加企业模型设置、不另存密钥。最小界面沿原 Campaign 编辑审批、老板搜索范围选择和客户详情安全进度，不把入组显示为已发送。
