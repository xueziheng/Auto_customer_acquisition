# composition_support —— 非进程机械装配库

只服务apps/api与apps/scheduler_worker的报价装配；不是第八个进程，不读取环境或启动循环。
允许依赖下层及本库，禁止绝对或相对导入任何apps进程模块。各进程不得互相导入。
共享代码不共享运行对象：每次调用创建独立parser、slot、Gateway、域服务与闭包；不缓存全局实例。
参数必须由进程入口显式提供，特别是tenant、来源/文件lease_owner和已构造的唯一approvals/engine。
只移动机械构造与原生命周期，不在此新增业务授权、金额、到期、通知或文件许可规则。
API/worker保留其公开factory签名和最终本层类型，同class重导出保持类型身份。
