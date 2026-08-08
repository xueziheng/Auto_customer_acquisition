# account_discovery/ —— 企业发现

## 职责

从假设出发找到具体企业与业务联系人线索：官网、公开目录、卖家店铺。Lookalike 扩展（找相似企业）也在这里。

## 输入 / 输出

输入：需求假设、目标市场、Playbook 排除国家。
输出：ChangeSet（create_account / create_contact_hint 条目，含来源 URL 与哈希）。

## 关键纪律

- 联系方式只产出「线索」，验证与法律依据留痕在 prospecting 域完成后才算联系人
- 企业名必须连同网站域名一起产出（消歧主键）
- C 端人群研究不在 Phase 1（见 ROADMAP 长期挂载点）

## 可用工具

web.search、web.read_page、contact.enrich（经 tool_gateway）

## 禁用工具

email.*
