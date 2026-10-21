# ADR 0118：平台模型权限、统一获客周期与同邮箱普通接续

日期：2026-10-22。状态：已实施，最终验收与独立评审状态见[验收记录](../operations/agent-upgrade-verification.md)。本决定不改变九条硬边界、商业逐次审批或冷序列终态。

## 背景

企业模型设置的旧老板保存接口可以改模型、调用上限和导出开关，违背平台独占模型配置的已确认要求。原工作区另有三档30天服务计划，直接运行旧持续研究驱动会与统一 InventoryRequest 重复探索，并把基础档10候选上限扩大为公共库存补充50/100。原自动 Campaign 首封只装配 Gmail，普通持续回复使用正式 QQ；仅证明手动 API 首封不能证明全自动链。

## 决定

企业 `save_nonsecret` 在域层拒绝所有角色；原 HTTP POST 保留但返回403且不更新版本。界面读取当前平台绑定的安全摘要，GET不创建运行pin、不读取密钥。平台 Catalog、凭证引用和旧运行pin仍由原平台路径管理；不新增企业绕过限额或修改导出许可的入口。

原服务计划契约、筛选证据、运营CLI和持久store语义迁入新分支。新增0102接在实际0101之后，四表强制RLS、租户角色授权并注册到租户建库检查。基础/标准/高级30天额度分别为候选100/300/600、搜索200/600/1200、联系人20/120/240；周期168/56/24小时；每轮搜索10/20/30、页面10/25/40、候选与假设10/20/30；单企业页面2/4/6、联系人1/1/2。切档不清零，显式到期续期才进入新30天；未知预留不退回。

周期在原singleton扫描中提交同一个 InventoryRequest。真实请求hash关联周期，库存即时命中亦有持久请求收据，不伪造 WorkflowRunId。原proposal授权不能自动晋升为已确认产品目标或发信许可，旧排程保留并要求重新确认；旧 ContinuousDiscoveryDriver 不装配。来源间候选/页面与搜索账本共享每轮上限，未知调用不可恢复成免费重试。既有库存fit也占候选处理上限。联系人按同一Campaign+客户共享处理收据，新准备和legacy find/verify复用；不同Campaign是独立处理，旧run收据仍优先识别，已预留上限不会因升档扩大。旧筛选等待仅唤醒对应step与代次，未增加第二个Provider循环或人工逐客户闸门。

原 `email.send` 保持所有业务检查，新增受信发送身份映射：当前获批 SendingIdentity、真实active员工的本人邮箱、当前客户owner求交集。技术tool用户与boss分类员不能冒充邮箱owner。准备阶段仅读数据库绑定事实；其指纹固定身份/邮箱操作版本，执行与对账再次核验，原连接器一次读取绑定后核对同operation再使用凭证，不降级换邮箱。既有Gmail与私人QQ授权/配置验证保持；全局自动发送关闭仍可准备/验证/入组，但实际邮件为零。

冷序列在客户回信后保持REPLIED，末步正常发送后保持COMPLETED，均不再发冷邮件。独立普通scope准入必须在admit自身重读精确SENT attempt、真实MIME/canonical关联、持久分类、已完成reply_qualification和可继续pending工作；COMPLETED另核对不可变版本步骤耗尽。其余stopped状态不准入。资料请求在原apply_actions仍running时，只允许真实分类/来源和当前scope预检后排入无binding的follow-up待办；不创建虚拟DialogueSource、不借owner权限读正文或运行普通模型。原流程完成后再严格准入；失败保留原review/handoff。客户回复可以引用该序列任一真实SENT步骤，不局限最后一步。

准入前与每轮共用`CurrentOrdinaryDialogueAccess.require_business_facts`，检查当前verified联系人/法律依据/国家类型、当前公开产品hash、本人reply身份以及联系人和客户抑制；直接admit及running预检拒绝时均不新增binding或普通待办，已有审计/待办保留。

普通reply仍由原 `email.reply` Gateway处理，逐轮检查真实owner、本人邮箱、当前公开产品hash、verified联系人、抑制、商业边界、额度及unknown。O1拒绝路径仅在双方与owner确实匹配时暂停原对话；伪造真实SENT header的他人来信不能暂停合法绑定。

## 代价与限制

新增周期/执行收据、唯一迁移和本人邮箱装配测试；保守预留可能少用额度但不会因失败未知自动返还。老板仍专有搜索/分配及Opportunity intake；sales可以验证同一Need，到机会节点需有权限真人接管。确认资料不等于公开产品或商业承诺。合成数据库/协议验收不证明生产已开启自动发信或真实获客成本有效。完整22项裁定和来源保留见验收记录。
