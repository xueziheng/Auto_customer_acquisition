# Web 核心运行时契约（Task 3a）

日期：2026-09-05。范围限本机受控装配的运行工厂、生命周期与健康；不是完整 Web 核心完成证明。

## API

保留零参数生产 `create_runtime_app()`：只在该 wrapper 读取环境。新增
`create_runtime_app_from_settings(settings, *, secret_resolver, object_store_settings,
model_client=None, manual_send=None)`，参数均使用现有明确类型；不读环境，不启第二连接池。
模型 client 显式注入时保持原 StructuredTradeManagerModelPort，不构造真实 Provider，也不
隐式回退。未注入时保留生产 OpenAI 默认。注入模型由调用者拥有，API 不关闭共享注入对象。

`build_phase1_dependencies` 直接调用仍兼容。返回 dependencies 明确提供 owned
`model_lifecycle` 与 `object_store_lifecycle`；直接调用者须关闭这些资源及可选 quotation
lifecycle，并自行释放传入 session factory 所属 engine。API 工厂统一代为管理。
生产 OpenAI 与旧上传 S3 的惰性 client 均提供幂等 aclose，仅关闭已创建资源。

同步构造阶段不得连接数据库、创建 Provider SDK 客户端或启动解析器进程。依赖构造失败时
没有已打开资源，不新增临时事件循环。真实 IO 从 schema/lifespan 或合法请求开始；启动失败、
取消、正常退出均尝试 quotation、owned model、owned object transport、engine 的全部清理。
有主异常则保留主异常；无主异常而普通清理失败则抛固定脱敏 RuntimeCleanupError；取消原样
传播。不得回显底层错误内容，也不得以成功退出掩盖清理结果未知。

## Scheduler

健康状态由原 worker 的窄 lifecycle observer 更新。配置/schema/数据库/registry 仅代表装配
完成；只有原专用 backend 取得单例锁、完成 activation 并确认仍为同一 backend 后，首周期
前才 running-ready。取得 session advisory lock 后首次 commit 取消、获取查询结果未知、
失锁或解锁未确认（包括 unlock 返回 false）时，使用锁查询前保留的原 driver 强引用，
先以有界 close 关闭，关闭失败或取消时 terminate 同一 driver，再 invalidate 包装器。
禁止 detach 后 invalidate 丢失物理关闭路径；禁止重新取得连接或在不同 backend 上假装解锁。
最终必须按原 driver 确认已关闭：未确认且无主异常时固定失败，有主异常保留原对象；
普通 unlock SQL 错误在原连接已确定关闭后可视为清理恢复。没有主异常时，清理取消在完成
物理关闭后仍向上传播；已有主异常时保留它，不能被清理取消覆盖。
原有 driver 顺序和所有锁确认点保持。observer 失败须固定脱敏，不能
阻止解锁或 factory 资源释放；不得增加锁轮询器或第二锁服务。

ready 同时读取该 worker 的 stop_event；设置停止标志即 not_ready，仍完成当前周期。
未获锁、失锁、启动失败、取消与停止均 not_ready，live 仍仅表示进程存在。保留未获锁与
失锁的非零退出语义。health server 增加显式 host，兼容原两参数构造；后续本机 wrapper
明确选 127.0.0.1。

## 阶段边界与验收

Task 3b 才负责完整 core services/readers/reply actions 的拓扑装配、requested-enabled
完整性与进程 bootstrap。3a 保留 direct SchedulerDomainDependencies 及 disabled 可选组；
不得增加通用 service dict、进程互导或完整依赖 factory。总体 Task 3 不得因此勾选。

先 RED 后 GREEN。真实自有 testcontainers PostgreSQL 必测锁竞争、activation、失锁、
stop flag、取消、observer 失败解锁、schema/registry 拒绝和真实 API 资源关闭。受控外部端口
只代替模型/SDK，不替代数据库或真实工厂。禁止读取现有数据库配置、.env 或真实凭证，
禁止真实 Provider/发送/供应商调用。提交前运行相关测试、ruff、mypy、边界检查和 diff check。

## Task3b：真实事实读取与有序装配

按 ADR 0025 补公开事实端口及精确四个共享 reader。资格读取必须每次读取当前域事实，
审批来自同一 ApprovalService 的 campaign:{id}:v{version}，不得 fallback。发送身份只调用
get/check_send_permission，不占额度。员工每次独立 scope，UserId 明确映射当前 active 员工。
联系人必须 email 且 tenant/account/contact 一致；类别只取 inferred/contacting 的有证据假设，
不得从行业或 Campaign 允许范围补造。撤销最后一条假设后类别为空，原资格门禁拒绝。
回复用 account 级保守规则，同账户其它联系人也暂停；未知入站失败关闭且不冒充 no_reply。
材料 reader 的可信上游是原 EmailSendHandler/Gateway：上游必须先取得并持续校验 canonical
当前 preflight/attempt 绑定。reader 只核对这个 preflight 的 tenant/contact/account/email 与
tenant-bound sender.get 当前材料，不是任意调用方的 Attempt 授权器，也不重建 Outreach。

业务组从显式 typed 端口构造；disabled/完整 enabled/configuration_error 为安全状态，组合
齐备不等于政策、审批、可达性或额度通过。研究、联系人、Campaign、既有窄回复、寻源、报价
按已有契约接线；Task5/6 未实现能力不命名新接口或宣称完成。直接旧装配继续兼容。

验证：真实 PostgreSQL 公共写入口建立事实，覆盖错租户/账户/联系人/身份、类别撤销、额度、
审批版本拒绝、unknown/auto/历史真人/人工纠正、disabled 零构造、enabled 缺项、canonical
单实例及 Task3a owned 资源与锁健康回归。外部 transport 可受控，业务域不得 fake。
