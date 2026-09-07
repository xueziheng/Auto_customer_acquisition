# ADR0066：受托回复的当前权限与原件 Gateway 读取

日期：2026-09-07。状态：本机受控修复，待限定复审。

## 问题

ADR0027 的受托员工检查只在后续动作发生，Raw 读取、模型调用和分类写入此前已经执行。
配置只绑定 tenant/employee，不能代表该员工仍在职或有 qualify 权限。

## 最小决定

- 保留 `require_reply_internal_access(action="qualify")` 的当前 active boss 下限。
  当前事实从原 EmployeeService 的受信 system 限定 lookup 取得；system 本身不授予内容访问。
- 复用 `inbox.message.evidence.read`：boss 的 tenant scope 已由当前 Inbox 员工事实支持，
  因而受托路径是 qualify 与 Inbox evidence 的权限交集，不扩大 manager/sales 或技术 review 权限。
  scheduler 单独构造原 handler/Gateway/PG ledger，借用原 bounded store，不改核心管线。
  Message/tenant/actor/task 绑定、EXECUTING 前审计、读取前后授权、完整性与 SUCCEEDED 后领取保持原状。
- 原 workflow 增加受信 `ReplyClassificationAccess` 窄端口，canonical factory 必定注入；
  原件之前、模型之前、模型返回后分别重读当前资格，subject_ref 与 message_id 必须一致。
  无受托员工的旧显式组合保留原端口兼容性；它们不成为 canonical factory 的 fallback。
- `record_classification` 增加可选受托 actor 契约。受托路径在原分类事务内先按已有
  Inbox 访问锁顺序锁 ownership/员工，核对同租户真实入站 Message 与出站关联，重核 qualify，
  再持 message 幂等锁并写分类/事件。员工锁保持到 commit，避免模型后检查与写入之间撤权。
  先提交的撤权拒绝本次写入；已获员工锁的分类先提交，撤权随后生效。旧原始分类接口不扩权。
- MIME guard、预算、投影、逐字证据、模型校验不变；不新增 Provider、不读取配置明文，
  取消/超时原样传播；既有幂等分类不能跳过本次授权。

## 验证与边界

真实 PG、Conversations、Gateway 和 scheduler 上验证排队后停用/降权/缺失/跨租户、
Raw 后撤权、模型期间撤权、审计失败、正常 ledger 与受控主链。
授权重核不是跨外部 IO 的分布式 fencing；已经获准开始的模型调用无法撤回已交付输入，
但返回后撤权禁止新的持久分类。真实 Provider、共享部署与桌面不在本次范围。
