# composition_support —— 非进程机械装配库

只服务apps/api与apps/scheduler_worker的报价装配及下述四个当前事实 reader；不是第八个进程，不读取环境或启动循环。
允许依赖下层及本库，禁止绝对或相对导入任何apps进程模块。各进程不得互相导入。
共享代码不共享运行对象：每次调用创建独立parser、slot、Gateway、域服务与闭包；不缓存全局实例。
参数必须由进程入口显式提供，特别是tenant、来源/文件lease_owner和已构造的唯一approvals/engine。
只移动机械构造与原生命周期，不在此新增业务授权、金额、到期、通知或文件许可规则。
API/worker保留其公开factory签名和最终本层类型，同class重导出保持类型身份。

## ADR 0025 当前事实读取扩展

除报价机械装配外，仅允许 employee_readers.py、campaign_approval_reader.py、
outreach_fact_readers.py、delivery_material_reader.py 四个公开事实映射/单次 scope 模块。
各进程独立实例；不得读环境、建 engine/registry/workflow、产生业务副作用、重写授权规则，
不得 import app 进程。地址仅 Gateway 材料端口消费，不得记录或进入模型/Workflow。

## ADR0026入站机械装配例外

仅email_inbound.py可构造本批email.inbound.fetch与email.inbound.raw.read的独立Gateway、
具名manifest registry、slot、Store及wrapper；每次调用独立实例，不建全系统registry、
workflow或engine，不扫描注册。tenant/route/当前授权端口/lease owner/session factory/外部
transport必须由进程typed显式注入；禁止环境读取、全局缓存、循环、业务角色规则与app进程import。
资源归原进程工厂对称aclose；构造不做IO。四个ADR0025 reader原禁止registry规则不变。

ADR0027允许email_inbound.py的InboundComposition公开其同一BoundedRawArtifactStore供scheduler
借用；与technical-review授权wrapper分离，不扩大review权限、不改变原进程资源归属。

## ADR0069 接管通知机械映射

允许 handoff_notifications.py 供 API 与 scheduler 复用既有 notice→持久 notification job 映射；
每个进程注入独立仓储/时钟，不读取环境、不建 engine、不投递渠道、不判断接管当前事实。
scheduler 保留原导出以兼容现有调用；当前事实与并发边界仍由机会公开 scope 与投递装配负责。

## ADR0070 模型机械装配

允许 model.py 显式构造独立 model.generate Gateway、handler、slot 和绑定 generator；
只消费进程注入的配置、凭证 resolver、当前授权端口、账本、配额仓储与 fingerprint provider。
构造不做 IO，不读环境、不建 engine/workflow、不判断业务角色、不导入任何进程模块。
各进程独立创建并对称关闭资源，不缓存跨请求或跨进程的正文、凭证与可变身份。

## ADR0071 会话机械装配

允许 assistant.py 构造独立会话/配置仓储、公开服务 scope 映射与上下文 builder。
只消费显式受信参数，不导入任何 apps 进程，不建 engine，不读环境，不启动循环。
权限与配置启用规则委托具名领域/运行时接口；不得在这里复制角色或范围判断。
