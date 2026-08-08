# upstream_nexscope/ —— Nexscope 上游技能原始留存

**只读目录。** 上游技能（Amazon、TikTok Shop、Ozon、Shopify、Temu、
1688、商品、卖家、公开网页搜索等 API 型技能，以及跨境市场、邮件营销、
利润、营销框架等文本技能）的原始文件按包存放于此，不做任何修改。

改写后的正式版本进 `../canonical/`，其 manifest 的 `upstream_ref`
指回这里的原始文件——保持可对照，上游更新时能 diff 出变化。

修改这里的文件 = 失去与上游对照的能力。要改，去 canonical 改。

导入时按 `../AGENTS.md` 的八包映射表归类。
