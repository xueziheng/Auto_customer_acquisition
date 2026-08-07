# 统一数据平面与 Provenance

「内置模型能处理系统里所有数据」不能实现成「每次把整个数据库塞给大模型」。正确做法是分四层组织数据，每次运行只加载当前任务需要的部分。

---

## 一、四层

### 1. 原始资料层

```text
邮件   聊天   截图   PDF   Word   Excel   图片   语音   网页快照   附件
```

经 `artifact-store/` 存取，**不可变**。每份资料记录文件哈希、来源、上传者、时间。

原始资料永不被覆盖或删除——它是所有结论的最终依据。模型摘要是派生物，不能替代原文。

### 2. 结构化业务层

```text
公司   员工   客户   联系人   需求信号   需求假设   已验证需求   商机
产品   供应商   任务   承诺   报价   成本   Campaign
```

这是业务真相，存 PostgreSQL。全表清单见 [10-database.md](10-database.md)。

### 3. 语义检索层

用 pgvector 支撑这类问题：

```text
客户以前说过什么          哪些聊天提到类似规格
哪些历史需求与当前相似     哪些产品可以替代
哪些员工处理过类似客户
```

**语义检索只用于召回候选，不用于下结论。** 检索出来的内容仍要经证据等级判断。

### 4. 来源与审计层

记录每个结论的出处：

```text
一个事实来自哪条消息        一个结论来自哪个网页
一个价格来自哪个 SKU        一个员工进展来自哪份上传
哪个 Agent 做出的推断       谁修改了结果
```

---

## 二、Provenance 规范

所有影响商业决策的字段都必须带（硬边界 4）：

```yaml
quantity:
  value: 5000
  source_type: conversation      # conversation | web_page | upload | employee_input | agent_inference
  source_id: msg_172
  extracted_by: model_v3
  confirmed_by: 张三
  confirmed_at: 2026-08-07T12:00:00Z
```

网页推断也要留证：

```yaml
need_hypothesis:
  value: packaging supplier
  evidence:
    - url: https://...
      observed_at: 2026-08-07T09:00:00Z
      page_hash: sha256:...
  inference:
    made_by: agent
    evidence_level: public_company_event
```

**注意这里没有 `confidence: 0.61`。** 置信度不存模型输出的数字，由代码推导（下节）。

老板在 UI 上点「为什么判断这是高意向」时，必须能一路点到原始证据。做不到这点的字段，等于没有这个字段。

---

## 三、证据等级 → 置信度推导

**这是硬边界 3 的落地机制。** 模型只判断「某条证据属于哪个等级」；置信度由确定性代码依据集齐的证据推出。

| 等级标识 | 权重档 |
|---|---|
| `agent_industry_inference` | 低 |
| `public_company_event` | 中低 |
| `employee_guess` | 中 |
| `customer_interest_reply` | 中高 |
| `customer_specification` | 高 |
| `customer_quantity_and_timing` | 很高 |
| `customer_sample_or_quote_request` | 极高 |

推导规则：

1. 取所有证据中的**最高等级**作为基准档
2. 同等级多条独立证据可上浮一档（独立 = 来源不同，不是同一页面抓两次）
3. 存在相互矛盾的证据则下浮一档，并标记 `has_conflict`
4. 证据超过新鲜度窗口（默认 7 天，按 Skill manifest 可调）则下浮一档
5. 结果是**离散档位**，不是小数

为什么要离散：档位可解释、可回溯、可测试。小数会让人误以为是测量结果，还会诱发「0.67 和 0.71 谁更值得做」这种无意义比较。

**实现要求**：推导函数纯函数、无 IO、有单元测试覆盖每条规则。输入是证据列表，输出是档位加解释文本。

---

## 四、事实与推断分离

数据结构上分离，不靠字段命名约定（硬边界 5）：

```text
observed_facts     直接观察到的（网页写了什么、客户说了什么）
inferences         由事实推出的（因此可能需要什么）
```

每条推断必须指向它依据的事实 ID。**无证据的推断在写入前被 `agent-runtime/guardrails/` 拦截。**

反例：不要出现「这家公司正在扩张，所以一定要采购我们的产品」这种把事实和推断焊在一句话里的字段值。

---

## 五、Context Builder

每次 Agent 运行只加载必需数据，并显式声明工具白名单与黑名单。示例与权限约束见 [03-permissions.md](03-permissions.md#四context-builder给模型的数据也要裁剪)。

装载优先级：

```text
1. 当前任务目标与 Boss Directive
2. 当前客户/机会的结构化摘要
3. 与当前需求最相关的历史证据（语义检索召回，限条数）
4. 相关产品与供应能力（按需求类别过滤）
5. 公司 Playbook 中与本次动作相关的规则
```

**硬性要求**：上下文有 token 预算上限，超出时按上述优先级截断，并在 Run 记录里写明截断了什么。悄悄丢数据比拒绝执行更危险。

---

## 六、修改留痕

员工上传的资料经模型提取后，必须保留完整链条，不能只存最终版本：

```text
原始资料 → Agent 提取版本 → 员工修改版本 → 最终确认版本
          （含修改人、修改时间、来源证据）
```

这样才能回答「这个数量是谁改的」，也才能评估模型提取质量随时间的变化。
