# 合规

合规不是发送前的一次检查，而是贯穿数据获取、存储、触达、留痕的约束集合。本文档给出架构要求；具体法律判断以各国现行规定为准，实现前请核对最新条文。

---

## 一、国家政策包

不同市场的规则差异必须数据化，不能写死在代码里——规则会变，也会新增市场。

每个国家一份政策包，至少包含：

```yaml
country: US
cold_b2b_email_allowed: true
requirements:
  - truthful_sender_info        # 真实发件人信息
  - non_deceptive_subject       # 非欺骗性主题
  - working_opt_out             # 有效退出机制
  - honor_opt_out_within_days: 10
personal_data_basis_required: false
notes: CAN-SPAM 适用于商业邮件
```

美国 CAN-SPAM 的核心要求是真实发件信息、非欺骗性主题和有效的退出机制。欧洲与英国的 B2B 规则更复杂：会因收件主体是法人、独资经营者还是自然人而不同，也因联系方式是职务邮箱还是个人邮箱而不同。**即使信息来自公开网站，涉及个人数据时仍需要相应的数据保护判断。**

因此政策包要能表达：

```text
是否允许冷 B2B 邮件            是否要求处理依据
主体类型是否影响判断            联系方式类型是否影响判断
退订处理时限                    是否要求本地代表
```

---

## 二、GDPR / 英国规则下的处理依据留痕

B2B 冷触达在欧洲常依赖 legitimate interest（正当利益），但**依赖它需要能拿出评估记录，而不是假设自己有**。

每个联系人必须记录：

```yaml
contact_legal_basis:
  basis: legitimate_interest      # legitimate_interest | consent | contract
  subject_type: legal_entity      # legal_entity | sole_trader | natural_person
  contact_type: role_based        # role_based（info@、sales@）| personal
  source: company_website
  source_url: https://...
  collected_at: 2026-08-07T...
  assessment_ref: lia_2026_001    # 指向已完成的正当利益评估
```

**这些字段是 Phase 1 就要有的。** 事后补要重新追溯每个联系人的来源，成本极高。

同时必须支持数据主体请求：查询、更正、删除、反对处理。删除请求要能跨表清理，但保留最小必要的抑制记录（否则删了之后又会再次联系他）。

### 联系人 Provider 的最小化边界

Hunter 联系人补全只能处理已经通过租户绑定、国家政策、Playbook 与抑制检查的数据；邮箱
验证只接受已经租户绑定、授权且未抑制的现有联系点。API Key 由 Connector 内部按
`HUNTER_API_KEY_REF` 解析，只能放在固定 Hunter host 的 `X-API-KEY` header；不得进入
URL、日志、异常、数据库或模型上下文。邮箱、姓名、职位和 source URI 通过 repr-disabled
typed DTO 与 task-local 一次性槽交接，durable ledger 只保存安全 handle。

Provider 返回的 `score` / `confidence` 不是校准证据，必须丢弃。邮箱验证的四种结果都
记录 UTC 检查时间和固定成本备注，严格缓存 30 天；451 隐私声明保留为 typed 事实，由
业务 workflow 执行删除/抑制，Gateway 不得擅自写业务状态。任何抑制、政策或读取依赖失败
都 fail closed；不确定的付费调用进入人工对账，禁止自动重复调用。

---

## 三、抑制名单

全局生效，跨 Campaign、跨发件身份、跨员工：

```text
退订请求      投诉（标记垃圾邮件）      硬退信
人工拉黑      客户明确要求不再联系      竞争对手 / 关联方
```

抑制粒度有两级：**联系人级**和**企业级**。客户说「不要再联系我们公司」时必须抑制整个企业，而不只是发言的那个人。

抑制名单查询在发送路径的关键路径上，不能异步。

Phase 1 只保存最小 append-only 抑制事实：`tenant_id`、typed target、typed reason、UTC `occurred_at`、安全 `source_ref`、幂等键和服务端创建时间。typed target 必须且只能是联系人 `ContactPointId` 或企业 `ProspectAccountId` 之一，调用方不能提交自由 scope 再拼任意 ID。

六个原因固定为 `unsubscribe`、`complaint`、`hard_bounce`、`manual_block`、`competitor`、`existing_customer_conflict`。SYSTEM 只允许前三个自动原因，并且 scope 必须绑定精确单一 target；人工原因由有权限的人工主体写入。

公共服务没有 delete、update 或 unsuppress API，数据库 trigger 同样拒绝 UPDATE/DELETE。隐私删除原联系人或企业记录后仍保留这份最小事实，避免再次联系。查询失败必须向上抛出，不得把后端错误解释为“未抑制”；实际发送前由 Tool Gateway 再做一次 fail-closed 查询。

新增事实、按 Enrollment ID 顺序锁定并停止全部匹配活跃 Enrollment、Action 与 `SuppressionAdded` outbox 在同一事务中提交。任一写入失败则整体回滚。

### 删除抑制与发送抑制的职责分离

Phase 1 有两类不能互相替代的抑制事实：

- `prospecting_erasure_suppressions` 保存 canonical 联系方式的稳定 HMAC-SHA256
  指纹，负责“删除后不可再次采集”。它不保存原值、联系方式类型或业务备注；写入后由
  数据库 trigger 禁止 UPDATE/DELETE。HMAC 使用独立的 privacy-suppression key，
  不能复用数据库、邮箱或模型凭证；轮换必须先迁移旧指纹的查询能力。
- `outreach_suppressions` 保存 `ContactPointId` / `ProspectAccountId` typed target 与
  固定 reason，负责“发送前不可放行”，并可停止匹配的活跃 Enrollment。

响应删除请求时，prospecting 在一个事务中追加 hash suppression、删除联系方式及其
法律依据，并在无剩余联系方式时删除联系人；企业事实和既有 outreach suppression
保留。前者阻止重新采集，后者阻止重新发送。只实现其中一个都会留下绕过路径。

### RFC 8058 one-click 退订

已发送邮件同时携带 `List-Unsubscribe` 与
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`。token 只包含可验证的 key ID、
随机 nonce 与签名；数据库只存 nonce SHA-256、精确 ContactPoint/Attempt、安全 key ID、
创建/90 天到期/消费时间，不保存 token 或 HMAC key。

- GET 只返回固定说明，永不产生业务写入；
- POST 必须是精确表单 `List-Unsubscribe=One-Click`，成功固定空 204；
- 有效 POST 在一个事务内消费 token 并写 ContactPoint 级 `unsubscribe` 抑制；
- 无效、过期、已用 token 对外不可区分，统一空 204；数据库/密钥临时故障必须安全失败，
  不能把“没完成”伪装成“已退订”；
- 轮换时 active key 用于新 token，旧 key 在既有 90 天 token 窗口内仍可验证；删除旧 key
  前必须确认已无未过期 token。

---

## 四、WhatsApp

**WhatsApp 不能作为未经授权的冷启动群发工具。**

官方 Business Messaging Policy 要求企业在联系用户前获得电话号码和相应 opt-in；企业主动发起会话要使用获批模板；用户最后一次发消息后的 24 小时窗口内才可以进行较自由的服务回复；自动化必须提供清晰的转真人路径。

**允许的场景**：客户主动扫码、客户通过网站点击咨询、客户在表单中同意联系、邮件客户主动转到 WhatsApp、展会客户明确授权、已有客户继续沟通。

**禁止**：从网络找到手机号后直接群发。

架构要求：每条 WhatsApp 联系必须能指向一条 opt-in 记录（`consent_records` 表），无记录则 Tool Gateway 拒绝发送。WhatsApp 属 Phase 3，但同意记录的数据结构 Phase 1 就建——它同时服务于邮件场景。

---

## 五、图片与内容权属

寻源过程会接触大量他人图片，规则必须硬：

```text
未知权属图片        不删除水印
未授权图片          不删除 Logo
仅可裁剪            网页 UI、空白、无关背景
授权图片            才可做受控生成式修复
AI 参考图           必须明确标注
处理后              必须检查结构、型号、颜色未被改变
```

最后一条容易被忽略：图片处理如果改变了产品的实际结构或颜色，发给客户就构成误导，即使技术上是「修图」。

每张对外使用的图片记录：来源 URL、观察时间、页面哈希、权属判断、做过什么处理、谁批准的。

---

## 六、供应商价格与诱导性报价

抓取的价格是 `indicative`，不能进客户报价（硬边界 7）。此外要主动识别并拒绝：

```text
诱导性最低价          远低于市场且无对应数量档
模糊区间              「$1–$10」这类无意义区间
单位不明              不说明是件、套、公斤还是箱
数量档缺失            不说明该价格对应什么起订量
币种不明              未标明币种或计价方式
```

寻源时最多保留三个合格候选，每个都要核对产品类型、材质、尺寸、型号、数量档、MOQ、计价单位、币种。保存网页快照、截图、观察时间和哈希。

---

## 七、Playwright 使用边界

```text
允许   读公开企业页面、提取公开目录、下载公开文件
       操作经授权的本地登录页面、页面结构变化时辅助分析

禁止   绕过验证码            绕过登录限制
       读取未授权 Cookie      违反平台限制批量发私信
```

每个租户、账号、Run 使用独立 BrowserContext，状态不共享。浏览器操作留 Trace（截图、页面快照、网络活动）作为证据链。

优先级顺序见 [04-tool-gateway.md](04-tool-gateway.md#七playwright-的位置)：能用官方 API 就不要开浏览器。

---

## 八、禁售与高风险类别

公司 Playbook 中的排除类别在 Tool Gateway 的 Playbook stage 强制执行：

```text
受管制医疗产品        危险品
需要我方不具备认证的产品
制裁国家与实体        知识产权高风险仿品
```

命中排除类别的需求假设直接丢弃并记 `compliance_blocked`，不进入触达队列。

---

## 九、审计不可删除

合规的最终防线是「能证明当时做了什么」：

```text
每次发送     发给谁、用哪个身份、内容是什么、依据是什么
每次拒绝     被哪个检查拦下、原因是什么
每次审批     谁批的、什么时候、看到了什么内容
每次删除请求  谁提出、何时处理、清理了什么
```

审计记录只增不改不删。相关表见 [10-database.md](10-database.md)。
