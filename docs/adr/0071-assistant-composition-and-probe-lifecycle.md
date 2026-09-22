# ADR 0071：会话机械装配与模型探测生命周期

状态：实施。日期：2026-09-22。

## 决策

允许 apps/composition_support/assistant.py 机械构造本进程独有的会话、配置仓储和公开事实读适配。
权限规则仍在领域服务和 agent_runtime 的具名适配中；不得导入 API 或 scheduler 进程，
不得建 engine、启动后台调度或读环境。model.py 仍只装配模型 Gateway。
API 与 scheduler 显式传入自己的 session factory、服务、配置、时钟和 HMAC provider。

模型调用仍只有 scheduler 持久流程；API 心跳仅表示自身装配版本。scheduler 只有取得
原单副本锁后才注册装配状态，扫描前刷新心跳，退出移除自己的实例记录。
管理员探测是固定无业务正文的 model_probe 轮次，使用同一 Gateway 和配额；
版本改变、取消或当前员工撤权后的迟到结果不得将新配置标为已验证。

探测完成在会话与员工行锁下重核领域授权，再原子记录完成与验证时间。
新配置只增版本；修改后要求两个进程以相同版本重启。未配置搜索不会阻止产品帮助或提案准备。
