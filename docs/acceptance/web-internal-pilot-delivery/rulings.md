# 本轮全部决策与代价

按原始时间顺序逐条保留，含撤回与更正；每条最后部分记录代价或理解错误时的风险。完整上下文见 [progress.md](progress.md)。

1. 原日志第 27 行：Ruling: 用户已批准这一步，直接设计并按SDD执行，不重复征求技能流程选择或登录实现许可 — 开发者明确既有授权优先且要求持续完成 — 若理解偏差会产生可撤销的本地实现返工。

2. 原日志第 28 行：Ruling: 在既有linked worktree新建codex/web-internal-pilot分支，基于已归档37e7649；不新建另一目录 — 复用已验证依赖且保留已完成分支指针 — 工作目录名称仍旧，文档须使用真实绝对路径避免混淆。

3. 原日志第 29 行：Ruling: 单租户loopback内测、同源cookie、scrypt、8小时会话等采用Spec技术默认；共享TLS与真实Provider后续独立验收 — 适合当前先Web本机验证，不要求用户透露凭证 — 后续共享部署有额外安全与运维工作，不可直接公网开放。

4. 原日志第 30 行：Ruling: 新profile首用要求操作者显式业务政策文件，测试使用独立合成政策且不初始化真实用户profile — HANDBOOK禁止AI默认评分/SLA/业务阈值 — 首用多一步配置，不能宣传零配置业务运行。

5. 原日志第 31 行：Ruling: 冷备份只允许停写后恢复到新owner，不提供覆盖恢复或自动数据清理 — 保证原数据可保留并验证对象/数据库一致 — 备份有停机时间且需要额外磁盘空间，本轮无备份加密/自动保留策略。

6. 原日志第 32 行：Ruling: 历史基线9318和Web411属于旧代码，不重复跑无关全库，按新改动覆盖测试并最终独立审查 — 成本与测试相关性要求 — 全库新版本通过不能声称；发现跨面影响再补测。

7. 原日志第 45 行：Ruling: CSRF改为以高熵session token作key的域分离HMAC推导，新增get_session返回当前expiry与CSRF；仍仅保存摘要 — 独立随机CSRF且只存摘要无法在刷新后恢复，GET轮换又破坏多标签且产生写操作 — 格式成为稳定安全契约，未来调整需版本化/撤销旧会话。已在Task1实现前/过程中通知实现者更新Spec。

8. 原日志第 47 行：Ruling: Task1管理接口采用已有账号username绑定，revoke_all(username=None)仅撤销bound tenant，发行/管理按tenant→account统一锁序；ADR使用首个空编号0067 — 0028已存在且全租户撤销必须与并发发行串行 — 登录管理吞吐受单租户锁限制，适合本机内测，扩大部署须重新评估。

9. 原日志第 48 行：Ruling: Task4新增明确LOCAL_IN_APP通知模式并留ADR0068，不借用受控专用mode — 就近AGENTS只允许controlled入口使用旧例外，内测需要准确表达站内实际送达 — 本轮不提供邮件通知，重要消息仍需用户打开Web；默认生产邮件要求保留。

10. 原日志第 49 行：Ruling: Web/API端口首建后固定，存储端口变化显式更新 — 用户书签和cookie来源需稳定，Docker存储重启端口可能变化 — 固定Web端口被占用时需操作者解决，不能悄悄换地址。

11. 原日志第 71 行：Ruling: 认证层只为login增加JSON/4KiB要求，业务写请求保持原router的MIME/空命令协议，统一强制Origin+自定义头+会话CSRF — 现有Blob上传采用真实MIME，且存在合法空POST，统一JSON门槛会破坏已有协议并复制业务校验 — 其他请求体保护继续依赖既有router；认证检查必须覆盖所有写路径，Task2/4测试验证。

12. 原日志第 79 行：Ruling: Task3采用存储容器全部停止后的物理卷冷归档，恢复要求相同镜像ID — 直接满足零写入一致性边界，避免运行PG目录复制或跨对象/DB非原子快照 — 备份体积和停机时间较高，跨版本逻辑迁移不在本轮。Docker官方cp文档确认stopped与tar流支持；所有权/具名卷真实性仍须实测。

13. 原日志第 81 行：Ruling: Task2补员工域公开纯创建校验与infra认证provisioning_scope事务端口 — 现有EmployeeService没有创建校验，CLI直接持有业务规则或复制私有限流锁会违反边界 — 增加小型公共接口与对应测试，后续HR功能需复用/扩展，不能把本机可信CLI当Web授权入口。更新Task2文件表、Spec和brief；ADR0067补充不占0068。

14. 原日志第 84 行：Ruling: 更正上一条provisioning_scope要求，只保留员工域纯校验；复用create_account(session=...)之后仍持有的tenant锁，再锁读经理并校验至同事务提交 — 实现者澄清原顺序未访问私有锁，控制者前一判断信息不足 — 已付文档返工，避免不必要公共API；Task2必须测试无效经理整笔回滚。撤回前一preflight的auth service扩展文件，Task1认证核心无需更改。

15. 原日志第 86 行：Ruling: cookie名改为tradeos_session_加已验证Origin端口 — 原环境与恢复环境可在同一127.0.0.1不同端口，cookie没有端口隔离，固定名会互相覆盖 — 技术cookie名称成为内部契约；端口变更需重新登录，稳定Web端口设计保持。增加同cookie jar双来源测试，不增加公共配置选项。

16. 原日志第 90 行：Ruling: logout成功契约采用204无正文，修正计划示例中的200 — 规格要求POST撤销且清cookie，未限定成功状态；实现已使用更准确的无内容响应并导出schema — 客户端应按成功状态处理而非强制解析JSON，Task4严格消费生成契约；仅计划文档调整，无生产修改。

17. 原日志第 123 行：Ruling: Task4新增scripts/pilot_web_supervisor.py承载三应用生命周期，run_web_pilot.py保持CLI解析/派发 — 完整健康/信号/OwnedProcess收口需要独立职责，已有controlled采用同形拆分，避免把所有流程塞进CLI — 新增一个内部模块需要纳入审查/测试；不改变用户命令或进程边界。

18. 原日志第 132 行：Ruling: Task4扩展notification health及scheduler config/runtime窄接线 — 旧health只识别controlled，旧scheduler无条件要求Gmail/DKIM而pilot禁止虚构外部配置 — 新增显式pilot-only typed/parser/factory端口，原生产/受控必填校验保留；缺DNS/发送配置必须真实失败，不得fake成功。代价是扩大兼容性回归面，若错误可能影响旧入口，因此Task4补旧默认与新缺项拒绝测试。

19. 原日志第 136 行：Ruling: Task4为S3配置新增显式from_pilot_environ，返回dev_mode=False且只接受canonical http://127.0.0.1:port — 原parser只有开发模式允许HTTP，借dev=True会模糊真实认证模式 — 增加connector config/AGENTS与严格地址回归，默认parser保持；代价是维护独立受限解析入口，未来HTTPS共享部署不能复用本机例外。

20. 原日志第 139 行：Ruling: Task4允许reserve_port设置SO_REUSEADDR以支持稳定端口快速重启，禁止SO_REUSEPORT并保留活监听者拒绝 — 实际停机后TIME_WAIT挡住原地址重启，属集成发现的Task3窄缺陷 — 需同端口真实重启及占用回归，若错误会影响唯一监听者保证。

21. 原日志第 156 行：Ruling: Task4 I2采用同源Web Locks序列化login/logout，另保留busy与generation fence，缺API固定拒绝 — 单页代次只能保护JS状态，无法排序其他标签迟到Set-Cookie；W3C规范确认合作同源异步排他锁 — 新增浏览器能力要求，当前只实测Chromium，旧浏览器可能不能登录；不把Web Locks当服务端权限或恶意同源隔离。Spec已同步，Task5操作说明需列支持检查。

22. 原日志第 194 行：Ruling: 最终接受经理排他行锁与历史87条lint告警作为本轮已知代价；保留tar合法根基线不足和console宽过滤两项非阻断Minor，不开启无必修项的最终修复波 — 独立全分支审查未发现生产绕过，直接功能断言成立，文档已明确撤回过强观测结论 — 后续可能漏掉归档拒绝条件或并发console告警的回归；扩大告警门禁前须补强这两项，真实争用出现后再评估经理锁。

23. 原日志第 195 行：Ruling: 采用finishing-a-development-branch的保留分支方式，不再询问合并菜单，也不重复同代码全库测试 — 已授权范围是本机Web实现与收尾，用户此前选择保留继续，开发者要求完成已有授权且只补受影响检查 — 不提供合并后或全库新版本通过的证据；分支仍需用户将来决定如何集成。

24. 原日志第 196 行：Ruling: 将本计划全部Markdown任务记录与完整Ruling原文归档到docs/acceptance/web-internal-pilot-delivery，核对哈希与Git提交后仅删除本计划scratch — 保留可审查决策/反转/成本并遵循SDD收尾，不让决策随临时目录丢失 — 原报告中的scratch绝对路径成为历史位置，README与manifest提供归档映射；diff包以提交范围和哈希重建，不复制冗余补丁。
