# Hunter 管理员接入诊断

账户验证与邮箱样本是不同范围。平台 `last_test_scope=account` 只表示固定 `/account` 的认证测试；
`sample` 表示固定 `hunter.io` Domain Search 样本。账户通过不能证明套餐的邮箱权限、邮箱有效性或业务就绪。

样本工具 `provider.hunter.sample` 是真人显式的一次诊断，经持久 Gateway ledger 的
tenant、permission、idempotency、rate_limit 检查和 EXECUTING 提交后才访问供应商。
域名、limit=10、offset=0 固定；不创建联系人，不进入发送序列，不替代 `contact.enrich` 的业务门禁。
连接器会筛选、去重并最多保留五条有效候选，所以内部计数表示保留候选数，不表示 API 全部结果总数。
邮箱、姓名及来源原文不进入工具输出或 ledger；只允许受信进程在成功登记后消费安全计数。

每个配置版本使用固定幂等键，同一 handler 实例只能准备和执行一次。撤权、取消、失败和未知结果均不在
该实例自动重试。再次诊断必须由真人明确决定，并使用明确的新测试配置；不能自动换键绕过未知结果。
密钥轮换和删除会清空测试范围、结果和时间。平台保存不自动启用业务连接器。

## 2026-10-11 已发生的诊断

- 真实 `/account` 验证通过；平台可以记录 `scope=account, status=passed`。
- 随后的单次样本调用进入了 Provider 并返回到 typed parse 后阶段，但 manifest 未声明输出 schema，
  实际非空输出在 `handler.output` 检查被拒绝，持久结果为 `failed_transient / reconciliation_required`。
- 原进程已退出，安全计数没有交付，不能恢复或猜测实际邮箱数量。样本状态仍为不确定。
- 已补齐 manifest 的输入/输出 schema，增加同实例并发、重复执行和伪造 prepared 对象拒绝，并用受控
  transport 测试验证计数、权限和错误分类。修复不改变旧 ledger，也没有触发第二次真实样本请求。

只有未来显式真实测试产生新的成功登记，才可报告对应样本计数。合成测试通过不能覆盖上述历史事实。
