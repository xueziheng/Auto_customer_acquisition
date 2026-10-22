# QQ历史角色迁移回归

本次仅修复隔离测试的schema前提，不修改生产tenant_security、已发布迁移或当前隔离门禁。所有动作在独立stage测试container内；主/QQ真实数据库与配置未操作。

原0080非空往返测试在旧schema上调用最新provision和全库metadata verifier，正确触发“数据库企业隔离配置不完整”。先复现RED（46.95秒），再改为head准备受限角色和合成行，退0080后用历史SQL核验实际角色、不可提升的标志、三个表的双策略与FORCE RLS、伪造GUC仍只见本企业、跨企业插入42501。0079撤权后的读取42501、非空0078回退拒绝、数据行、列/CHECK/FK/触发器不变和最终策略断言全部保留；未删断言让测试通过。

独立新增0086前向角色测试：head创建受限角色→降到0086（未来dialogue表已不存在，0081两表仍有四项DML权限）→升head→完整当前隔离verifier。此路径验证本次QQ0086升级到0102时现有角色的新表授权，而非仅在新schema后创建角色。

服务器执行TASK/venv/bin/python TASK/jobs/pg_targeted.py，参数为test_qq_fixed_tenant_roles.py中的上述两个节点。runner仅从私有测试配置取得task-owned disposable DB地址并脱敏输出，不读取运行profile。最终2通过/39.90秒；结构检查与ruff通过。日志位于stage .superpowers/task-4/。

## 已知旧回退限制

额外探索路径head→0080→0079→0080→head发现reply_classification_resumptions和reply_classification_retries两表缺DML重授：二者由0081创建，降到0080会删除，0081重建时未重授旧角色。当前完整隔离门禁正确拒绝该结果。缺表安全元数据与失败均已保存在privilege-diagnostic.log，未把它当作已修。

按控制者明确裁定，不为本次新增0103、不改已发布0081。本次实际0086→0102路径已由独立回归通过；降至0080再经0081重建不是本次回滚方案，禁止使用。本次回滚应恢复冷备0086，具体备份、迁移和运行切换由控制者执行。

本结果不代表全套或真实邮箱/Provider验收。没有真实发信，所有邮件相关测试继续仅模拟。
