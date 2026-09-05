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
