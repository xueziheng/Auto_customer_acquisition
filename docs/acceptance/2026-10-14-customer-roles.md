# 企业客户权限与精简工作台验收

日期：2026-10-14。工作树：candidate-inventory，分支：codex/candidate-inventory。本轮起点 bc60958；最终代码提交 `57f2e21b1a18d47a1ccba59c1d1e2445012acf55`，已部署。

## 权限与数据

|操作|平台 xue|企业管理员 jslt|员工 qihao、qikai / 普通 manager|
|---|---|---|---|
|33 个获客来源的访问、配置和策略|现有平台管理入口|无配置权限|无配置权限|
|手动搜索客户|不冒用企业身份|允许|拒绝|
|公司待分配池和显式分配|不新增跨企业业务操作|允许，目标限同公司在职人员|拒绝|
|本人客户跟进|不替企业审批|仅自己实际负责的客户|仅自己实际负责的客户|
|开发任务边界和审批|不替企业审批|沿现有管理员审批流程|无修改权限|

新交付先进入公司待分配池。已有负责人保留；分配给本人或同事均不重复消耗取得额度。相同负责人重试幂等；已有其他负责人时拒绝覆盖，本轮未添加转派。历史客户缺档案映射时，由管理员显式同负责人关联。

## 已取得的本地证据

- Task 1：83 项相关单元/PostgreSQL 测试通过，覆盖权限撤销、原归属保留、并发分配、批量回滚、额度与租户隔离；修复员工行锁与归属外键死锁后独立复审通过。
- Task 2：初始相关组 601 项通过；独立审查找出的自动装配、发件身份轮转、回复准入三项问题已修复。修复后的覆盖组 446 项通过，无跳过。两组有重叠，不相加计数。
- 自动跟进默认关闭，正式 API 与调度器共享启用条件；就绪判断与实际轮转一致；新入组在占用额度前核验当前回复。原始幂等键重放继续返回原结果。
- 已有联系人、来源假设、批准任务、发送前复核等约束保留。公开邮箱未被写成已验证联系人；测试未创建生产政策、批准或发送记录。
- Task 3：初始全前端52文件582测试通过；修复后的6个相关文件73测试通过。修复了各403入口清理不一致、迟到分配回填，以及第101条未知跟进容量异常使批量卡住的问题；独立复审通过。两组不相加。
- OpenAPI 类型重新生成，vue-tsc/Vite 构建成功，结构七项检查通过；改动前端文件lint无输出。全库前端lint既有1057 warnings、0 errors；PlatformConsole静/动态导入提示保留。认证测试当前2条ECONNRESET、基线1条，13测试均通过，尚不能称为纯既有噪声。
- 最终前后端整体审查完整读取76文件、13,058行差异，发现2项Important（浏览器存储失败仍提交、需求假设状态改变阻断原请求重放）和1项Minor（搜索轮询清除分配选择）；三项已在57f2e21的单次修复波次解决，覆盖后端377项和前端78项均通过，最终针对修复的独立复审Approved，三项均ADDRESSED，无新增阻断。
- 已记录而未纳入本次清理：认证测试额外ECONNRESET因果、Alembic配置弃用提示、Vite导入提醒。当前证据未确认产品回归；若这个判断错误，可能掩盖后续故障，需要维护时继续定位。

## 最终修复验证命令

后端（Python 3.12 venv）：

```bash
python -m pytest -q tests/unit/test_customer_followup_application.py tests/unit/test_customer_followup_permissions.py tests/unit/test_outreach*.py tests/integration/test_customer_followup_api.py tests/integration/test_outreach*.py tests/integration/test_current_outreach_facts.py
```
结果377 passed、16.39s、无skip。原命令核对覆盖需求validated/证据变化、活动暂停与版本变化、联系人取消验证、运行关闭、审批撤销；当前身份/归属变化继续拒绝，新key仍走完整准入，原结果不重复计额。

前端（apps/web）：

```bash
npm test -- tests/customer-workbench.test.ts tests/prospect-inventory.test.ts tests/customer-assignment.test.ts tests/workspace-navigation.test.ts tests/customer-discovery.test.ts tests/navigation-routes.test.ts
npm run build
```
结果78 passed、4.72s，vue-tsc/Vite成功。存储读取或写入失败在POST前停止；未知结果用原key/body核对，当前选项变化不丢原记录。10秒poll仅更新任务/额度，分配窗口由显式刷新或提交后刷新更新，后台始终现查归属。

## 生产与真实 UI

### 发布及数据保留

- 旧发布：`/srv/tradeos/releases/20261014-navigation-cf80ec3`，原数据库版本0074。
- 新发布：`/srv/tradeos/releases/20261014-customer-roles-57f2e21`；归档SHA256：`95bba7f84c9b2b58144971ce90cbcd7a1778a80a5fe6d635cf327a5fd20d62df`。本地/服务器摘要一致；发布包只有已提交代码与生产dist，排除浏览器资料、私有配置和scratch。
- 北京时间13:21:55开始停止服务；官方stop、backup均完成，`verify_backup`完整校验成功（3个受校验文件）。备份路径：`/srv/tradeos/backups/20261014-customer-roles-pre-57f2e21`；旧发布与备份保留。
- 官方migrate成功，数据库0075；13:23:51 systemd恢复active/running、Result=success。官方状态显示storage/applications均running，公网`/api/health/ready`返回ready。
- 迁移前、迁移后、UI显式关联档案后：取得1、已归属1、当日用量1/50。取得ID/owner联合摘要始终为`66c335684318c05ccaa1515321e43c94e6bf0bc35ef702fca8ad4d2fbb9d114d`。通过受租户限制的只读审计只返回计数/摘要，未输出凭证。
- 未操作`/srv/tradeos/development/TradeOS`，未删除客户记录、未改发件身份或模型限额。

### jslt 登录后的真实页面

- 已登录Chrome直接访问新版；主导航为找客户、我的客户、消息、产品资料、企业管理。旧供应卡/寻源/成本报价/开发任务/商机/需求证据/运行恢复入口不再作为日常并列导航；相关历史深链保留。
- 找客户：显示今日剩余49、已取得1/50；关键词、非洲市场、数量和国家输入正常。实际操作了关键词/市场及qihao目标员工选择，未提交搜索或分配空列表。来源面板、技术预算、全市场库存总数已移除，历史搜索折叠。
- 待分配0家；下拉可见同公司在职jslt、qihao、qikai及既有测试员工。生产没有可用于正向新分配的待分配记录，所以本次不声称在生产将新客户分给员工；该行为由实际PostgreSQL/HTTP/前端隔离测试证明。
- 我的客户保留旧Simba公开候选，显示仅本人；详情负责人为“我”。通过UI点击“关联客户档案”成功绑定原负责人，刷新后映射仍在；公开邮箱未被创建成已验证联系人，未新增跟进。
- 详情真实显示：联系方式待验证、客户类型待确认、产品适配证据待补充、自动跟进未启用、公司无活跃开发任务。启动按钮禁用，公开来源仍可追溯。
- 企业管理保留企业配置、团队、邮箱连接。团队实际显示jslt为企业管理员、qihao/qikai为员工。没有以员工账号登录，也没有读取密码或复制登录状态。
- 实际点击消息、产品资料、我的客户导航并等待对应路由，分别进入智能收件箱、企业资料库和我的客户；没有读取或发送邮件正文。公开来源面板可展开，包含原始来源链接、观察时间及“需求尚未验证”标记。
- 实际列表/详情/搜索三页在1440与390宽度各检查一遍：documentWidth等于视口宽度，越界input/select/button均为空；并查看桌面和手机上下滚动截图。
- 上线后清除旧版控制台记录再开始本次验收；最终浏览器控制台实际输出 `Total messages: 0 (Errors: 0, Warnings: 0)`。

### 真实截图

下列截图来自正式服务器jslt会话，不是本地Mock：

- [找客户（桌面）](assets/2026-10-14-customer-roles/search-desktop.png)
- [我的客户（桌面）](assets/2026-10-14-customer-roles/customers-desktop.png)
- [客户详情（手机上部）](assets/2026-10-14-customer-roles/customer-mobile.png)
- [跟进条件（手机下部）](assets/2026-10-14-customer-roles/followup-mobile.png)

## 验证边界

本轮真实 UI 不提交额外采集、不提高模型额度、不向客户发邮件。员工和跨公司权限由隔离的服务、HTTP、数据库与前端测试验证，不能写成已登录 qihao/qikai 的线上实测。

自动跟进是否可执行取决于真实联系人验证、客户类型及适配证据、有效审批、发件与回复配置。显示阻塞原因属于正确失败关闭，不代表生产自动收发信已验收。
