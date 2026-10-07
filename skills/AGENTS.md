# skills/ —— 技能资产

## 职责

技能 manifest 与 prompt 资产。**技能是数据不是代码**：新增技能 = 新 manifest + prompt 文件（插件点 2），`skill_router` 自动发现，零代码改动。

## 结构

```text
skills/
├── manifests/schema.yaml    manifest 的权威 schema
├── canonical/               本项目的正式技能
│   └── <skill_id>/
│       ├── manifest.yaml
│       └── prompt.md        prompt 资产（不进 manifest）
├── upstream_nexscope/       Nexscope 上游技能的原始留存（只读）
└── references/              整理规则与映射文档
```

## 一次任务只加载所需

技能不是全量注入系统提示词的。`skill_router` 按 trigger 精确匹配加载，manifest 里的 `allowed_tools` / `blocked_tools` 参与 context_builder 的工具交集计算——**技能加载是权限边界的一部分**。

## Nexscope 上游整理规则

设计稿第二十八节的八个技能包映射：

| 目标包 | 吸收 Nexscope 能力 | Phase |
|---|---|---|
| Demand Intelligence Pack | 市场研究、趋势、商品评价、店铺/卖家查询、竞争分析 | 1 |
| Account Discovery Pack | Shopify/TikTok/Amazon/Ozon/Temu/Walmart 店铺卖家、公开网页搜索 | 1（web 部分）/ 2（平台 API） |
| Audience & Creator Pack | 目标用户画像、创作者发现、Influencer | 3（C 端） |
| Outreach Pack | 邮件营销、外联模板、跟进序列、个性化框架 | 1 |
| Product & Supplier Research Pack | 产品研究、1688 搜索、以图搜款、商品比较 | 2 |
| Product Presentation Pack | Listing 能力 → 仅用于产品卡/客户资料/多语言内容，**不用于自动铺货** | 2 |
| Pricing & Trade Cost Pack | 利润计算框架、定价策略、物流与到岸成本框架 | 2 |
| Compliance Pack | 专利、商标、图片版权、品牌风险 | 1 |

整理原则：上游 prompt 原样存 `upstream_nexscope/`（溯源），改写后的正式版本进 `canonical/`，manifest 记录 `upstream_ref`。**上游文件只读**——改了就无法对照上游更新。

## Nexscope 覆盖不了、必须自研的技能

需求信号本体、需求假设管理、B2B 企业发现、联系人验证、Campaign 管理、回复识别、需求确认、CRM、员工分配、承诺账本、老板指令、审批、Provenance——这些是 TradeOS 的域与工作流，不是 prompt 能解决的。

## 三个 canonical 示例即模板

`demand.infer_buyer_need`、`outreach.draft_discovery_email`、`sourcing.verify_supplier_candidate` 三个写全的 manifest 是后续所有技能的模板。新技能照抄结构。

## 评估

技能的评估样本在 `tests/evals/`（全库唯一评估集位置），manifest 的 `eval_refs` 指向它们。改 prompt 必须重跑对应样本。
