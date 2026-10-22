# 2026-10-14 客户转交与阿里邮箱阶段验收

## 用户目标与真实结果

用户要求通过 UI 将 jslt 找到的客户交给 qikai，由 qikai 使用 sales@jslingte.com 联系，并修复途中问题；用户确认邮箱为阿里企业邮箱。本次明确授权覆盖这一客户的实际联系，不延用上一轮“只读、不转交、不发信”的测试范围。

已在生产 UI 将 Simba Group of Companies - Nigeria 转给 qikai。转交后详情显示“当前负责人：qikai”，jslt 的“我的客户”显示“暂无本人客户”。真实公司记录并不等于已识别具体个人；公开 info 邮箱仍未验证。

阿里邮箱连接表单与受控发送连接器已部署。尚未取得 qikai 的真实邮箱连接事实，尚未发出客户邮件，也没有声称自动回复已可用。已将测试 Chrome 留在本人邮箱页面的登录入口，请用户直接登录 qikai 并通过网页连接邮箱，密码不得进入聊天或工具输出。另询问 jslingte.com 是否兼作主业务域，以确定实际发送方式。

## 发布

- 代码：`9cd32cc`，包含前一提交 `921d83f` 的显式转交入口。
- 发布：`/srv/tradeos/releases/20261014-customer-mail-9cd32cc`。
- 冷备份：`/srv/tradeos/backups/20261014-customer-mail-pre-9cd32cc`，官方备份后验证 3 个文件。
- 官方 migrate 完成，schema 仍为 `0075`；服务状态 running，公网 readiness 为 ready。
- 在 profile 内显式创建服务用户私有 `aliyun-mail` 目录；不把凭证打包进发布物。
- 发布前后、转交之前的客户归属摘要一致，今日搜索用量为 1 / 50。

## 实现与受控测试

转交原子更新取得记录和 OwnershipLock，留历史，不额外扣搜索额度；检查原负责人和分配时间，持账户锁拒绝活动跟进、在途或待对账邮件。旧发现任务须提交原归属版本，归属已变或历史绑定缺失时拒绝，不补造。

阿里认证仅通过固定官方 TLS SMTP/IMAP 和 Gateway；绑定以 tenant、employee、user 隔离，错误和日志不含秘密。email.send 保留原发送检查，只在执行阶段解析当前员工精确地址的连接器。部署明确的 Gmail 地址保留旧路由，无失败 fallback。SMTP 不确定结果只对账，不自动重发。

已验证：新增 8 项真实 PG 转交并发测试、阿里认证与隔离 PG 测试、发送 API 回归、32 项发送 handler 测试、阿里连接器回归；前端客户/邮箱 3 组 30 项通过，生产构建通过；结构自检、diff whitespace、修改文件的类型检查通过。API 路由清单漏更新导致的两项测试已修正，相关 38 项重跑通过。全仓导入展开的 mypy 仍有本次修改范围外的既存错误，不声称全仓类型检查已通过。

## 尚未完成的真实发送链

1. qikai 登录和本人阿里邮箱认证，需要用户直接在 UI 完成。
2. 客户页当前没有把指定公开邮箱登记为 ContactPoint 并实际验证的操作入口。平台 Hunter 配置存在且账户测试 passed，但不等于该邮箱已验证，也不等于 standalone 的 contact.verify 已装配。
3. standalone 的 DNS step/resolver 仍未配置，发件身份的 SPF/DKIM/DMARC 认证不能由手工改状态替代；实际 DKIM selector 需要真实依据。
4. 现有 Outreach 仅支持 cold_outreach，主业务域不能靠改标签绕过。首封手动联系尚需明确的准入路径，不能默认为启动自动 Campaign。
5. 真实来源、国家、客户类型、产品适配推断及处理依据须可追溯；CountryPolicy/Playbook 也须接入准入。不能自动生成评估引用冒充评估、把推断写为客户需求，或把公开邮箱当作已验证。

后续继续从真实状态完成，不重复转交，不重复扣额度，也不绕过当前未满足的发送条件。
