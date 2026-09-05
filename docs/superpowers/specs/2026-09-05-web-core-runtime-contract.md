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
失锁或解锁未确认时，先让原连接脱离池，再 invalidate 原物理连接；禁止将可能持锁的连接
回池，禁止自动重连后在不同 backend 上假装解锁。解锁清理取消不得覆盖既有主异常。
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
