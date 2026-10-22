# ADR 0097：真实单客户手动邮件运行装配

- 日期：2026-10-14
- 状态：按用户明确授权的企业内客户转派、QQ单封UI测试实现；真实发送验收另记。

## 问题与决定

客户转派和邮箱连接成功不能证明联系人可达，也不代表发件身份、审批或自动收回复已就绪。
本次复用已分配客户的真实网页原件，员工在本人客户详情确认公开资料及适配推断；
公开职务邮箱仍需真实处理依据和独立Hunter验证。资料与需求假设不能转成客户已验证需求。

Enrollment的execution_mode保持明确、不可变。manual只解除自动运行组合要求，不解除任何
当前owner、联系人、需求证据、精确Campaign审批、抑制、回复、发件身份或额度门禁。
scheduler持久查询排除manual；手动发送只通过原prepare/send与email.send Gateway执行。
迁移0076降级在检查前取表锁，存在manual数据则拒绝，避免并发写入失去人工意图。

管理员可配置供手动联系使用的审批边界；这个配置视图不修改Campaign持久语义。
真正不被后台自动挑选由Enrollment.manual保证。自动用途仍必须满足原完整运行组合。

## Hunter真实配置、预算与隐私

独立部署只接受显式hunter-customer-settings文件。引用已有真实configured/validation_passed
的精确非秘密配置版本，先实际构造contact.enrich及contact.verify两个manifest，再追加
runtime_composed；不把平台诊断passed字段当业务验证证据，不自动声明配置或调用/account。
凭证仅固定逻辑resolver由Gateway handler调用时读取私有文件，不进入模型或HTTP响应。
API只暴露本人客户的单邮箱verify。联系人发现原Playbook和国家政策仍按真实公共服务检查；
没有对应政策不放行，不新增或冒充国家验证Action。

contact.verify沿原tenant→permission→suppression→rate_limit链，额外处理依据检查只消费
已经记录的真实字段。抑制精确检查联系点和真实所属客户，缓存不能绕过客户级抑制。
真实typed结果经Prospecting公开方法落库；risky、invalid、unverified绝不能标成VERIFIED。
已持久的privacy_refused优先本地清除，即使Provider关闭或超过缓存期，也不再把地址交给Provider。

批准一个狭窄的持久技术预算例外：具名contact.verify rate_limit在租户PostgreSQL advisory锁
下，只追加安全reservation/cache_hit事件。当前received及未知执行保守计入显式UTC日预算，
保持原10次/秒、300次/分频控；有效缓存不扣预算。取消、提交未知或Provider未知不退款；
无当前ToolCallId的旧reserve调用必须关闭。事件不保存地址、正文、密钥或原异常。
此例外不允许其他CheckStage写业务状态，不改变Gateway主管线或stage顺序。
后续worker若启用同一能力，必须显式注入同一持久quota实现，不能另开内存额度桶。

## QQ公共DNS装配

独立scheduler显式启用QQ DNS时只接受qq.com与经公开原邮件头核实的s201512 selector。
复用原dns.auth.check handler、四阶段、持久ledger、判定规则与认证状态机；每个查询有界。
真实DNS变化照常失败，非QQ请求明确拒绝。旧pilot默认未配置，不开放任意域或selector。
DNS检查只证明配置符合解析策略，不证明某封邮件的SPF IP、DKIM签名或DMARC alignment。
发件身份仍经管理员UI登记、请求检查、刷新核对和明确启动真实日期预热，不伪造认证或active。

## 验证与限制

受控PG/Gateway测试覆盖manual调度排除、原发送门禁、迁移并发保护、精确readiness、四种验证结果、
缓存、账号抑制、隐私删除恢复及持久预算并发/取消/未知；外层Provider transport使用fixture。
生产真实DNS另有公共检查记录，联系人验证和邮件SMTP接受须通过UI另行验收。
SMTP接受不等于送达。QQ历史邮箱不导入；本次记录只涉及TradeOS产生的业务操作。
