# Tool Gateway

**所有对外部世界的动作只能经过这里。** 模型和 Agent 永远拿不到数据库密码、邮箱 Token、WhatsApp Secret、浏览器 Cookie、企业微信 Secret、对象存储密钥（硬边界 1）。

Gateway 不是「一个转发函数」，而是一条检查管线。它是最后一道闸——即便上游判权有漏，这里独立再查一次。

---

## 一、调用路径

```text
Agent / 工作流
      ↓ 提出工具调用（工具名 + 参数）
┌─────────────────────────────────────────┐
│  Tool Gateway 检查管线                   │
│  1  租户校验        tenant_id 一致       │
│  2  身份与权限      RBAC + ABAC          │
│  3  Playbook        公司规则与排除类别    │
│  4  国家政策包      目标国家的合规要求     │
│  5  抑制名单        退订/投诉/拉黑        │
│  6  审批状态        高风险动作是否已批准   │
│  7  幂等            幂等键是否已执行过     │
│  8  频率与配额      每日上限、并发         │
│  9  成本预算        Phase 3 接积分钱包    │
└─────────────────────────────────────────┘
      ↓ 全部通过
执行 handler（内部持有凭证）
      ↓
记录 tool_call：入参、结果、耗时、成本、证据
```

**任一 stage 拒绝即终止**，并返回结构化拒绝原因（不是抛裸异常）——Agent 需要知道「为什么不行」才能改用别的办法，而不是重试同一个调用。

---

## 二、Stage 的顺序有讲究

先做便宜且否决率高的检查，再做贵的：

- 租户和权限是内存判断，最便宜，放最前
- 抑制名单和幂等要查库，放中间
- 审批状态可能需要等待人工，放在执行前最后一道

**幂等必须在执行前、成本记账后。** 顺序错了会出现「重复扣费但没重复发送」或更糟的「重复发送」。

---

## 三、工具 manifest

新增工具 = 一个 manifest + 一个 handler，**不改管线**（插件点 3）。

```yaml
id: email.send
version: 1.0.0
description: 通过指定发件身份发送一封邮件
risk_level: high            # low | medium | high
cost_class: low             # 决定 Phase 3 的积分预留
requires_approval: true     # 含价格内容时强制
idempotency: required       # required | optional | none
required_permissions:
  - outreach.send
checks:
  - tenant
  - permission
  - playbook
  - country_policy
  - suppression
  - approval
  - idempotency
  - rate_limit
inputs:
  sending_identity_id: string
  contact_id: string
  subject: string
  body: string
outputs:
  message_attempt_id: string
audit:
  record_payload: true
  redact_fields: []         # 敏感字段在审计中脱敏
```

`checks` 是显式列表而非隐式默认——读 manifest 就能知道这个工具受哪些约束，不用读实现。

---

## 四、风险级与审批

| risk_level | 例子 | 要求 |
|---|---|---|
| `low` | 读公开网页、搜索、读本域数据 | 记账即可 |
| `medium` | 写业务数据、创建草稿、抓取供应商页面 | 记审计，受配额限制 |
| `high` | 发邮件、发通知给客户、生成客户可见报价、修改发件身份 | 必须审批或在已批准 Campaign 边界内 |

**Campaign 边界内的自动发送不需要逐封审批**，但下列内容永远需要（见 [AGENTS.md](../../AGENTS.md#六必须人工审批的动作)）：首次具体价格、正式报价、折扣、库存承诺、交货期承诺、认证承诺、付款条件、合同条款、独家代理、质量保证、目录外产品的客户参考价。

判断规则：**只要输出会构成对客户的商业承诺，就必须人工过目。**

---

## 五、幂等

外部动作重试是常态（网络超时、Worker 崩溃、状态机重扫）。没有幂等就会重复发信——这既毁客户观感，也毁域名信誉。

- 幂等键由调用方生成，语义上等于「这件事」：`{tenant_id}:{tool_id}:{业务实体 id}:{步骤序号}`
- 键记录在数据库（不能只放 Redis——Redis 丢键等于重复发送）
- 命中已完成的键：直接返回原结果，**不重复执行、不重复计费**
- 命中正在执行的键：拒绝并提示稍后，不排队

---

## 六、凭证归属

凭证只存在于三处，Agent 与模型都碰不到：

```text
Vault / KMS          服务端密钥
macOS Keychain       桌面端本地凭证（Phase 3）
Connector 内部       运行期从密钥服务取，不落日志、不进上下文
```

审计记录里对凭证类字段一律脱敏。**日志不打印完整请求头。**

---

## 七、Playwright 的位置

浏览器操作也走 Gateway，且优先级最低——能用 API 就不要开浏览器：

```text
官方 API → 公开 HTTP 页面 → 确定性 Playwright Adapter → 受限 Browser Agent → 人工接管
```

**适合**：读公开企业页面、提取公开目录、下载公开文件、操作经授权的本地登录页、页面结构变化时辅助分析。

**禁止**：绕过验证码、绕过登录限制、读取未授权 Cookie、违反平台限制批量发私信。

每个租户、账号、Run 使用独立 BrowserContext，互不共享状态。详见 [08-compliance.md](08-compliance.md)。

---

## 八、可观测性

每次工具调用都要能回答：谁在什么时候、为了哪个 Run、调了什么工具、参数是什么、结果如何、花了多少、留下了什么证据。

这既是审计要求，也是老板点「为什么判断这是高意向」时的证据来源（硬边界 4）。相关表见 [10-database.md](10-database.md)。
