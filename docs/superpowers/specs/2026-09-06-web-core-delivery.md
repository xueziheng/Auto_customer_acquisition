# Web 核心交付与桌面扩展契约详细规格（Task13）

日期：2026-09-06。基线：`aa24508dc77fac1e6cc2e21225cb3dcd21dd240b`。
本批是文档交付与最小隔离恢复演练，不改变业务、迁移、权限、审批或根九条硬边界。

## 输入与口径

[正式设计第六节](2026-09-05-web-first-completion-design.md)定义五种客户端边界。
[Task12正式验收](../../acceptance/2026-09-05-web-core-completion.md)为当前行为依据：
48e4465完整9318通过；7e10383仅两个测试修复、Mac完整主链1通过，不能合计或声称后者全量。
Task0–12独立审查已完成，Task13和最终全分支审查尚待完成。

## 交付文件与接口

| 文件 | 本批内容/边界 |
| --- | --- |
| README、ROADMAP、HANDBOOK | 逐项同步本机Web闭环；历史实现顺序与真实运营标准分开；保留已验Phase2事实 |
| docs/operations/web-core-local.md | Python3.12+/Node24安装来源、四进程、HUP、owned停止、预算、恢复和不可用能力 |
| docs/operations/web-core-capability-matrix.md | 每页/API/执行者/真实组合/disabled与not_run；迁移0059；精确版本证据 |
| docs/architecture/12-client-capability-boundaries.md | 文件、SecretResolver、Notification、Browser、能力发现的owner/授权/输入输出/失败；只引用真实接口 |
| docs/acceptance/web-core-delivery/ | 安全Task报告/裁定历史快照/必要截图与索引；根最终追加裁定，不先写最终通过 |
| domains/conversations/service_impl.py、tests/integration/test_conversations_correction.py | 仅纠正Task7两个旧docstring：无权/跨租户/不存在统一PermissionDenied，授权后未分类才ValidationError |
| apps/AGENTS.md | 仅修正浅骨架等事实状态，不改变任何规则或worker授权 |

无新公共接口、事件、字段或迁移；桌面为未来适配约定，不建Tauri/空目录/IPC/设备自报授权。
Task9详细spec完整安全保留。裁定记录保留每条Ruling理由和成本，进度历史不当最终状态。

## 最小备份恢复验收

原入口无备份脚手架。复用现有Supervisor/OwnedContainers/ControlledConfig，只新增独立integration演练。
源为本轮新建隔离PG+MinIO，目标为另一新建owned空资源；禁止接受任意配置/外部dump/运行库参数。
源在无应用写入者时通过真实RawArtifactStore写合成原件及metadata，PG整库备份到内存，恢复到
目标空PG；原件从源bucket复制到目标bucket。查询显式tenant过滤，比较安全metadata及读取原件SHA256。
不输出连接/密钥/对象key/raw内容；ControlledConfig.read仅脚本内存解析。数据库操作串行。

每次资源操作前核owner标签和精确ID；原Supervisor迁移子进程使用PID出生核验；finally先关连接再
精确清理两个owned资源和私有配置，只保留安全JSON。失败不打印原始异常/locals/dump。
恢复失败不能标通过；目标必须空且owner不同，拒绝同目标/已有业务内容。先补有意义反例，实际恢复
证明元数据/原件一致及清理，不复制生产backup服务或完整launcher。

验证界限：不证明运行中跨PG/S3一致性快照、PITR、外部邮箱游标灾备、生产迁移恢复或自动接管。
停止原launcher会删除owned数据；本演练不会恢复已有运行库。真正持久部署须先另行设计备份保留与恢复门禁。

## 文档与证据验收

相对Markdown链接必须存在；命令只引用真实入口。主矩阵逐行消除Task0“需补组合”过期口径，
旧Task0清单如保留须作为明确历史文件。四应用HUP与PG随机HostPort变化分别记录。
Mac无完整quotation/自动寻源准入；独立Linux不同owner/Need完成报价/PDF，不能混成统一入口。
真实Provider/Gmail/供应商联系/共享登录/桌面/通用Agent任务源/Catalog培养消费者均如实记录。
成本/token/人工工时未知，不填0。选安全代表截图并实际查看，已跟踪Task12证据优先复用。
六张Catalog重生成截图先安全归档再精确恢复至BASE；其它历史output不动。

检查：新增恢复聚焦测试、文档链接/状态一致、结构边界、敏感扫描、改动文件ruff/diff-check；
不重复Task12未变全量。source与report明确提交，禁止git add .、push、merge、部署或真实外发。
