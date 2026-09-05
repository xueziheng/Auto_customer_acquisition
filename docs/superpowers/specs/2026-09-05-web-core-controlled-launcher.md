# Task4：本机受控 Web 启停契约

本子规格只覆盖 A1/A9 的启动底座。CLI 为 `python scripts/run_web_core_controlled.py`，
可指定三个 loopback 端口（0 自动选择）、运行目录父路径；前台 Ctrl-C/TERM 停止。
运行目录每次以随机 owner 创建，0700；配置0600、禁止加载 .env 或继承业务环境。
状态只含 owner、阶段、容器ID、进程PID/出生时间、公开URL、固定失败类别；绝无配置值。

## 分层与冷启动

root scripts 监督资源与进程；infra/controlled 仅资源生命周期、严格配置与受控外部响应。
apps/api/controlled.py 与 apps/scheduler_worker/controlled.py 各自构造生产 runtime，禁止进程互导。
API 提供受控模型和 Gmail transport；scheduler 复用 CanonicalSchedulerBootstrap 和受控 DNS。
只初始化本 tenant 的持久 Employee/UserId/角色/经理映射（两名独立老板，另含经理、销售、寻源、产品）。没有 Tenant 实体表时仅使用新 tenant ID。
不得插入业务、审批、认证、预热、需求、机会或邮件结果。Playbook/政策仍走提案与独立审批。
发件身份登记/预热Web入口待Task8，入站与完整回复待Task5/6。研究/联系人/寻源/报价外部组
没有明确完整场景时保持 disabled；Task12 扩展场景时仍用同一启动入口和typed ports。

配置使用严格不可变模型，只能读取本 owner 私有文件。基础设施密码内部随机生成，
受信 resolver 只解析本配置中的具名引用；模型无该对象。受控模型仅对操作文档中明确的中文演练输入返回预定义研究提案响应，仍经过原Guardrails与schema；
未知输入拒绝，不虚构用户未选择的业务预算或批准。研究执行外部组缺失时禁止确认执行。Gmail 响应保存在 owner 独占 SQLite 外部场景库，
只含 Provider 响应，不读业务PG；API/scheduler重启共享同一库，整体新启动绝不复用。
DNS只为具名受控域返回合成 TXT，其余拒绝。Python进程socket审计只允许本次PG/MinIO
目标和loopback监听，未知地址/端口在实际网络调用之前拒绝；不声称操作系统安全沙箱。

## 资源与失败

Docker API 使用固定本机 socket；新 PG/MinIO 用 owner label+完整ID，端口发布核验127.0.0.1。
不 pull、不复用容器/数据；迁移只能发生在本次创建且核验身份的PG中，app只检查当前head。
API listener由父预占并传递FD，scheduler/Web strict bind失败，绝不杀端口占用者。
每个子进程独立session，owner记录PID和出生时间；信号前核验二者与进程组。
TERM只设置worker停止标志，完成当前cycle；超时才对核验后的owner组KILL。
所有清理逐层尝试：进程→容器→私有配置与场景；任何未知清理非零，主错误优先保留。
失败日志只保留固定类别，不保存raw stdout/stderr/异常/config；状态诊断文件保留。
支持同owner的应用重启（监督器命令，保留PG/MinIO/场景），完整停止删除本owner数据。

## 必须的证据

先失败测试后实现；真实子进程验证端口占用、缺依赖、阶段失败、worker崩溃、TERM、重启；
核对实际容器发布地址与所有owner资源清理。独立安装声明依赖后启动，不能依赖其他工作树.pth。
前端DEV+显式owner配置角色选择限定本次员工，仍fixed-dev；generation失效与PROD拒绝测试。
健康只由当前HTTP检查得出；组件展示受控模式与已知disabled能力，不能声称真实服务验证。
