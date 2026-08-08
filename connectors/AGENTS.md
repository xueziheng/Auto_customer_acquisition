# connectors/ —— 外部系统适配器

## 职责

协议转换：把外部系统（Gmail、验证服务、搜索、企业微信…）的 API 变成本系统的接口形状。**不含业务规则**——「什么时候该发邮件」在 `domains/outreach`，「怎么调 Gmail API」在这里。

## 新增连接器 = 新目录，零核心改动（插件点 1）

```text
connectors/<name>/
├── AGENTS.md      能力范围、合规约束、密钥归属
├── client.py      实现对应的 Protocol
└── manifest.py    注册元数据（能力、限流、密钥引用名）
```

## 三条铁律

1. **凭证只在 connector 内部。** 运行期从密钥服务（Vault / 环境变量）取，不进参数、不进返回值、不进日志（硬边界 1）。日志不打印完整请求头。
2. **不直接写业务表。** connector 返回数据，由域服务落库——绕过域就绕过了 Provenance 和租户过滤。
3. **只被 tool_gateway 和 worker 调用。** 域和 agent_runtime 不直接 import connector。

## 错误分类

对外调用失败必须分类后抛：认证失败/参数错 → 不可重试；网络超时/限流 → `TransientError`/`RateLimited`（带 retry_after）。分类错了要么重试风暴要么静默放弃。

## 目录清单与 Phase

| 连接器 | 职责 | Phase |
|---|---|---|
| `gmail/` | 发送、拉回复、退信/投诉回调（**深，参考实现范式**） | 1 |
| `email_verification/` | 可达性验证（硬边界 6 的执行者） | 1 |
| `contact_enrichment/` | 联系人补全（单 Provider） | 1 |
| `web_search/` | 公开搜索 | 1 |
| `playwright/` | 浏览器操作（合规边界见 docs/architecture/08） | 1（受限） |
| `fx/` | 汇率快照 | 1 |
| `files/` | 本地/上传文件接入 | 1 |
| `dns_auth/`（在 gmail 内或独立） | SPF/DKIM/DMARC 校验 | 1 |
| `outlook/` | 第二邮箱体系 | 2 |
| `supplier_data/` | 1688 等供应商数据 | 2 |
| `trade_data/` | 海关/贸易数据 | 2 |
| `wecom/` | 企业微信（含 OpenClaw 插件对接） | 2 |
| `obsidian/` | Obsidian Vault 只读接入 | 2 |
| `whatsapp/` | WhatsApp Business（仅 opt-in 场景） | 3 |

## Phase 1 之外的目录

只有 AGENTS.md（能力范围、合规约束、密钥归属），没有代码——空壳 client 会让人以为能用。
