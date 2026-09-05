# ADR 0025：当前发送事实与有序运行装配

状态：已接受；适用 Task3b，本机受控范围。

公开读取缺口由所属域提供窄 DTO/查询，不由 app 扫描全租户数据或猜事实。
Prospecting 增加精确 tenant/account/contact 法律依据与资格投影；地址另由 Gateway 材料读取。
Demand 以 SQL 的 tenant/account 和 inferred/contacting 状态过滤，在有证据的假设上提取类别；
最多 200 个不同类别，第 201 个导致固定失败。validated/rejected 不再属于冷触达假设资格。
Conversation 采用更保守的 account 级暂停：任一未分类入站为 unknown；任一当前有效非
AUTO_REPLY 分类为 replied；否则 no_reply。后来的 AUTO_REPLY 不覆盖另一条历史真人回复。
人工纠正按 corrected_at + correction_id 最后记录确定有效分类。整个读取是单条 SQL 快照，
发送前仍沿原 prepare_send 重读，不承诺阻止查询之后才提交的入站。

共享库仅增加 employee_readers.py、campaign_approval_reader.py、outreach_fact_readers.py、
delivery_material_reader.py 四个有实际复用需求的公开事实映射/单次 scope 模块。无环境读取、
engine/registry/workflow 构造、业务副作用或授权算法。API 可以同类重导出，进程独立实例。

Scheduler 增加 typed 分阶段 bootstrap：同池 canonical core → readers → 唯一 outreach →
reply/account/demand → handlers/engine/outbox。保留直接 dependencies，禁止第二池、mutable
service dict 或 app 进程互导。已请求组必须完整，否则固定配置错误；未请求组不构造外部端口。
Task5 自动正文入站和 Task6 完整回复动作以及 Generic Agent/Browser 保持 disabled。
地址材料只在 Gateway 内短期存活，不进入日志、模型、Workflow 或审计。

运行身份的 UserId 与审批/归属的 EmployeeId 不可强转。API 发现 Run 使用当前 EmployeeView.user_id；
指令确认使用原确认人 decided_by_id 对应的当前 active 老板 user_id，重复确认不替换执行身份。
worker 按当前 tenant 唯一 active user_id 映射再核角色，未知、重复、停用、错租户都拒绝；不改
历史 Run，不做同串 fallback。旧显式依赖入口缺少映射 reader 时失败关闭。手动发送既有
ToolCallContext 的 EmployeeId 契约维持，由其原授权器校验，不改成新的 UserId。

DeliveryMaterialReader 仅是内部可信材料端口：原 EmailSendHandler/Gateway 必须先取得并持续
核验 canonical 当前 preflight/attempt 绑定；reader 对精确 tenant/contact/account/email 和
tenant-bound sender.get 逐项核对，不授权任意 Attempt，不反向调用或重构 Outreach。

运行能力 DTO 只声明本进程装配状态；enabled 不代表商业政策、审批或发送门禁已通过。
API 与 scheduler 独立生命周期，模型及外部端口的 borrowed/owned 关闭规则沿用 Task3a。

Fix1 收紧实际装配：原 API ProspectingDemandAccountNames 机械迁移至已允许的
outreach_fact_readers.py，原 API 路径重导出同类；唯一 Demand 同时连接目录事实与账户发现
必需的 name/country/domain 读取。DirectiveService 只由 scheduler runtime 构造一次，经
core.directives 交给研究和寻源，避免各消费者各自建域服务。
