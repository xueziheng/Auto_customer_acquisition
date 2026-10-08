# connectors/obsidian/ —— 企业知识资料任务快照

继承 connectors 与根总纲；本次实现见 ADR0081、企业隔离实施计划 Task3。

## 已实现范围

`workspace.py` 将受信上游已经授权的 UTF-8 Markdown 字节生成单次只读任务目录：
`root/tenant_id/employee_id/run_id/document-NNNN.md`。它不扫描、导入或同步已有 Obsidian
vault，不读产品数据库，不负责检索，也不直接启动 Codex。网页上传与资料问答需由受信应用/worker 装配并分别验收。
当前没有 Obsidian Sync 或完整 Obsidian connector 能力，不得用此模块的通过测试替代上线验收。

## 技术边界

- 企业、员工、任务由服务端身份决定；每份文档绑定完全相同 scope。DTO 构造成功不是业务授权。
- 调用前必须重读员工在职状态、企业与客户负责人范围，确认产品发布状态，裁剪字段并过滤凭证。
  文档只读字节由上游已有 Artifact Store 完整性验证后提供；connector 不直接读业务表。
- root 必须部署为受信服务用户所有的 0700 目录；禁止路径中的软链接，禁止提供任意来源文件路径。
  程序生成文件名；来源名中的路径、隐藏名和 AGENTS.md 被拒绝，内容永远视为不受信资料。
- 拒绝混合企业、员工、任务、重复 live run、重复来源版本、非 UTF-8、空内容、NUL 与错误 SHA-256。
  文档数量和总字节数有明确上限；工作目录 0500、文件 0400。
- `PreparedKnowledgeWorkspace.verified_path()` 在交给执行器前重新核实精确目录 inode、文件集合、
  文件 inode/单链接、权限及内容完整性；其返回路径只在 async context 存活期间有效。
- 正常、异常、取消退出只清理本次持有的 inode，不递归遍历或删除其他资料。
  目录/文件被替换或出现额外文件时拒绝清理未知内容，固定标记清理未完成，交由受信运维处理。
- 清理是有界同步操作，无 await/后台线程，不因取消而遗留继续运行的写入线程。
  进程崩溃/断电不保证自动清理；没有后台清扫器，不得宣称任何故障都无残留。

## 执行器责任

只接受受信调用栈交付的 workspace 句柄，调用 verified_path 后仅挂载该目录；
不得把共享父目录、数据库、密钥、其他企业目录或旧共享 vault 暴露给 Codex。
CLI 仍须独立临时会话、禁止项目配置/规则提升、清洁子进程环境和最小只读文件系统；
网络工具与外部动作只能经过原 Tool Gateway。目录 Unix 模式不是模型沙箱，也不防同 UID
恶意进程/root。运行前及结果交付前再次核来源授权，引用撤权后不得复用答案。

## 企业 Markdown 投影

`vault.py` 只供受信 worker 发布企业专属、仅追加的 Markdown：`tenant/Docs/document/v<revision>-<status>.md` 与不可变 `tenant/Sources/document.md`。仅接受 awaiting_confirmation / confirmed，两种状态不等于产品发布。路径来自受信身份与版本，模型不得提供路径。root 与子目录固定服务 UID/0700，文件0400；逐层 O_NOFOLLOW、单链接和内容完整性校验，同版本同内容幂等、异内容拒绝。先私有完整写再原子创建最终名字，从不覆盖已存在版本。上游负责发布前后重核资料权限与期望 revision，旧 worker 不写 current 指针。该模块不直读数据库、不解析原件、不自动确认事实，也不部署 Obsidian 桌面或 Sync。
