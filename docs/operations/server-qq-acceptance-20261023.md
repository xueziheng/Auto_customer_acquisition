# QQ 服务器 Agent 升级与模拟验收（2026-10-23）

## 交付位置与范围

实际 QQ 源码已切换至 `/srv/tradeos/acceptance/qq-20261019/src`，运行代码提交为 `f60563024997e01b8fa6d6cc12f66e47f9888f3e`，分支 `codex/tradeos-qq-agent-integration`。后续最终提交只补本报告和来源结果，未改运行逻辑。基线为服务器 QQ `5b6474a`，合入昨日升级 `682c541`；不是用 T7 旧目录覆盖服务器。

保留原 UI、QQ 询盘/回复/跟进、企业私有知识与 Obsidian 协议、平台管理模型的权限、既有来源与设置。核心链路为确认本企业产品 → 产品/地区任务 → 持久获客与恢复 → 证据与联系人检查 → 获批触达 → 普通对话 → 需求与人工接管。Codex CLI 为运行器；私有资料、授权与外部动作仍由租户隔离和 Gateway 约束。普通邮件对话与原 QQ 验收路径的双向去重已合并，报价、交期等承诺仍走审批。

## 测试证据

以下全部在服务器独立 checkout 运行，外部模型/搜索/邮件使用受控替身，另列真实来源诊断。计数有重叠，不应相加为独立用例总数。

| 检查 | 实际结果与解释 |
|---|---|
| 首轮完整 Python unit/evals | 10146通过、9失败、7跳过；原失败日志保留 |
| Python修复后定向回归 | 208通过；覆盖原9个失败及受影响邮件/MIME、浏览器/进程生命周期路径；额外浏览器补测13通过。没有声称重新跑了一轮全量全绿 |
| 首轮QQ/Agent PostgreSQL | 179通过、1失败；失败为0080历史schema测试使用当前schema校验 |
| 历史权限及0086→0102回归 | 最终2通过（96.15秒），含3表×8项真实ACL破坏检查、同租户成功写入回滚、跨租户拒绝；未弱化生产隔离门禁 |
| PostgreSQL补测 | 50通过、1个teardown错误；业务断言通过。另一组并行迁移授予全cluster角色权限，临时库依赖阻止DROP ROLE；独占重跑受影响用例1通过（67.07秒）。临时测试容器最终已清理 |
| 获客来源受控组合 | 88通过，含11类网页策略逐ID真实Collector/DiscoverySession调度；11个JUnit属性警告不影响断言 |
| 原生Codex CLI | 5通过；真实Linux二进制/bwrap，合成本地模型端点，不代表真实模型质量或收费实测 |
| Web | 66文件703项通过；lint 0错误/1213既有警告；类型检查、构建通过，OpenAPI重新生成无差异 |
| 静态检查 | 结构边界、ruff通过；mypy覆盖968个源码文件通过 |

首轮7个跳过中5个原生Codex显式开关用例随后单独执行通过；macOS专属用例及owned PG/MinIO opt-in验收未在本服务器执行。不能把这些写成通过。

## 实际切换与保留

切换前停止QQ服务，完成并验证冷备份，再迁移数据库0086→0102。原主环境仍为0078，主进程PID切换前后相同，主来源配置文件哈希不变。QQ原始model/knowledge/platform settings的哈希不变。

主环境实际存有4组来源配置与23条备注，QQ原来没有。受信宿主通过原配置仓储写入QQ控制租户，逐值验证保存结果，记录新的版本与审计；主环境文件只读，密钥不输出。没有把原控制租户ID和旧额度/通过状态直接复制给新租户。

切换后：QQ核心服务active；API、scheduler、notification均HTTP200/ready；Web HTTP200，4个构建资源引用吻合。用户要求本轮只模拟邮件，因此运行时使用独立`profile/agent-upgrade-simulation-settings.json`：reply_enabled=false、automatic_send_enabled=false、qq_reply=null、reply_classifier_employee_id=null。原model-settings保留。旧business-mail worker停止并禁用开机启动；原已失败的business-inquiry worker保持未运行。手动UI邮件入口未被全局移除，本次没有触发它们，也没有发送真实邮件。

独立运行依赖仍位于 `agent-upgrade-20261022/venv`，不要作为测试垃圾删除。任务专用PostgreSQL容器已删除，主环境和QQ存储容器未删除。

## 33 个来源与实际限制

[逐项验收表](server-qq-source-results-20261023.md)及[机器可读结果](server-qq-source-results-20261023.json)记录全部33个已选来源。18个原本尚未接入自动获客；11类网页策略通过受控链路验收，但真实搜索后端/预算配置尚未启用。

免费接口诊断共3次HTTP：TED返回1条公告样本；Hunter账户认证和额度读取成功；DeepSeek仅检查账户元数据，未证明免费赠金额度资格所以未搜索；Tavily因缺少免费专用账户配置未运行。没有模型、页面或真实邮箱调用。这些结果不是“33个来源全部联网获客通过”。现有DeepSeek密钥与平台模型配置得到保留，但QQ实际`research`与`inventory_search`缺失，真实获客仍需平台管理员配置搜索绑定与预算。

## 回退资料

| 内容 | 服务器位置或Git引用 |
|---|---|
| 切换前QQ源码 | `/srv/tradeos/acceptance/qq-20261019/src-before-agent-20261022`；原分支`codex/jslt-enterprise-access`：`5b6474a` |
| 当前主环境源码备份 | `codex/server-current-20261022`：`dd023e4` |
| 昨日升级输入 | `codex/tradeos-agent-upgrade`：`682c541` |
| QQ完整冷备份 | `agent-upgrade-20261022/rollback-profile`（已verify_backup） |
| 原私有profile文件、启动脚本 | `agent-upgrade-20261022/profile-files-before-agent`、`agent-upgrade-20261022/start-before-agent.py` |
| 切换/健康/测试记录 | `agent-upgrade-20261022/logs`；历史角色最终回归另在stage `.superpowers/task-4/review-green.log` |

GitHub只保存源码与报告，不含私有配置、密钥、数据库或对象存储。回退应停止QQ核心与邮件进程，按原CLI的restore流程从冷备份还原存储，恢复原源码/启动配置并核对迁移号与健康状态；不要直接将0102数据库与旧源码混用。旧0080以下回退再升级存在历史重建表授权缺口，本轮未修改该历史路径，详见[历史角色回归说明](server-qq-historical-role-regression.md)。restore会创建新profile、端口和存储资源，启动时须使用其生成的config.json及资源绑定，不要用旧config.json整体覆盖恢复结果。用户尚未进行真实邮箱验收，恢复环境时也不得自动恢复发信worker。
