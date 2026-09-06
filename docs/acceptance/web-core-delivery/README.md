# Web 核心正式证据与裁定索引

日期：2026-09-06。当前工程行为以[正式A1–A10验收](../2026-09-05-web-core-completion.md)及
[主能力矩阵](../../operations/web-core-capability-matrix.md)为准。Task0–12独立审查已完成；
Task13与最终全分支审查在本快照截止时尚待完成，最终裁定由控制者追加，不从历史进度文字推导通过。
未推送、合并、部署或真实外发。

本批实施与局部门禁见[Task13报告](task-13-report.md)及[源码提交清单](source-commit.json)；独立审查仍待。

## 当前可核证据

- [Task12最终独立双Approved](task-12-review.md)与[实施报告原文](task-12-report.md)：
  48e4465完整9318通过；7e10383仅两测试I1修复后完整Mac主链1通过，不累计。
  报告原文顶部“等待复审”是当时快照，由最终review替代；历史失败不删除。
- [完整后端结果](../../../output/acceptance/task12/backend-final.json)、
  [安全摘要](../../../output/acceptance/task12/backend-final-safe-summary.json)、
  [1630源码快照](../../../output/acceptance/task12/final-source-snapshot.json)、
  [98失败逐项修复索引](../../../output/acceptance/task12/backend-resolution-index.json)、
  [I1修复源码](../../../output/acceptance/task12/i1-source-commit.json)、
  [I1静态门禁](../../../output/acceptance/task12/i1-static-gates.json)。这些安全JSON已在正式仓库output归档，优先复用。
- [Mac精确proof](../../../output/acceptance/task12/8d234c0956fb498e9f171342c3b72f51/proof.json)、
  [Mac清理](../../../output/acceptance/task12/8d234c0956fb498e9f171342c3b72f51/cleanup.json)、
  [Linux owner审计](../../../output/acceptance/task12/backend-final-linux-owner-audit.json)。
- [静止owned备份恢复](backup-restore.json)：仅源PG/原件→另一空owned目标，元数据/SHA256一致，
  同owner和非空目标拒绝、源不变、精确清理。不是用户业务backup/restore接口，命令及限制见[操作说明](../../operations/web-core-local.md)。
- [Task9详细spec](task-9-spec.md)正式保留；本批规格见[Task13详细spec](../../superpowers/specs/2026-09-06-web-core-delivery.md)，
  桌面无实现，只有[现有接口及未来责任](../../architecture/12-client-capability-boundaries.md)。

## 实际查看的代表截图

Task12实施者/root/独立reviewer已查看以下四图，Task13再次直接查看。只证明图中可见状态，
不以文件名或图片替代HTTP/数据库/PDF断言。图中金额均为合成演练，非供应商真实报价。

| 图 | 可见内容与界限 |
| --- | --- |
| [Mac Need 1440](../../../output/acceptance/task12/8d234c0956fb498e9f171342c3b72f51/need-1440.png) | 客户表达、逐字段来源/原件入口可读；目标价格不是客户可见报价 |
| [Mac Handoff 390](../../../output/acceptance/task12/8d234c0956fb498e9f171342c3b72f51/handoff-390.png) | 窄屏待接管队列、等待排序和推断提示可读；并非accepted截图，接受由真实proof证明 |
| [Linux Quote 390](../../../output/playwright/t10-2ea6b4a4e32f429192c2d1926a2ebfc1/quote-390.png) | 指定V2已批准、basis/对象绑定可读；PDF实际下载/解析由原完整测试证明 |
| [Linux 精确成本1440](../../../output/playwright/t10-2ea6b4a4e32f429192c2d1926a2ebfc1/exact-cost-1440.png) | quoted产品采购来源、精确成本单和22项适用清单；与Mac为不同owner/Need |

历史视觉证据另保留三张，Task13逐张实际查看后从临时目录精确复制，SHA见manifest。
它们只对应原Task报告版本，不拼成Task12同源码证明：

| 历史图 | 实际可见状态及验证边界 |
| --- | --- |
| [Task8 Inbox原件390](task-8-filled-inbox-evidence-390.png) | 合成消息/Artifact引用、下载入口、有效分类；“规则要求不代表已执行”提示 |
| [Task9 Settings未知390](task-9-settings-unknown-390.png) | 候选可能已保存、Run尚未确认、核对历史后按原请求恢复按钮；属于该轮受控HTTP故障视觉证据，真实candidate commit故障由Task12独立PG测试证明 |
| [Task11成本缺项390](task-11-run-cost-inputs-390.png) | 合格机会未知，token/工时/费用/单位成本未知及缺项；仅演练调用统计，不是运营效率或实际价格 |

六张被全量测试重生成的已tracked Catalog截图已逐张直接查看，只见合成策略、pending、
queued培养和stale状态及安全ID；归档至[catalog-full-suite-side-effects](catalog-full-suite-side-effects/)，
不是额外运行的验收。原六路径精确恢复到Task13 BASE，其他Task10/12历史output保留。
归档SHA和BASE SHA见[manifest](archive-manifest.json)，不覆盖过去测试数字。

## 裁定与历史快照

[完整progress历史附录](rulings-progress-history.md)精确保留每条Ruling、理由、成本、deferred/parked
以及失败检查点；它是本计划记录，不是新增规则入口。只复制安全Markdown，不复制真实config/DSN、
Cookie、SQLite、dump或完整原始敏感日志。旧报告中/tmp路径是历史定位信息；当前交付结论的证据已由
本索引的正式路径承载，不要求这些临时路径永远存在。

快照截止：`2026-09-06T11:53:12.025277+00:00`；Ruling所在行数 `87`（不是问题数量）。
[archive-manifest.json](archive-manifest.json)记录来源路径、源/归档SHA256与字节数，全部精确复制。
历史快照中的Markdown路径保持原文，需按来源目录解释；下表与主索引提供正式访问入口。
[Task0能力历史](task-0-capability-history.md)只额外重定位了相对链接，不能当当前能力表。

| 历史原文 | 精确源/归档 SHA256 |
| --- | --- |
| [rulings-progress-history.md](rulings-progress-history.md) | `fcfd353b7fa5b4993a77f806b4e4b01a6cc79974c7eafb140c0a1f28758998e0` |
| [task-9-spec.md](task-9-spec.md) | `579f976556a72fb5c1ad8089ea83fc83a14283f1d04a5414793eaf0136b4619f` |
| [task-0-report.md](task-0-report.md) | `d9404bf8a89fb7dd78fd1e4848c1e1a0ffdf96b66a5cbf627c2a31ca8d9b61c2` |
| [task-0-review.md](task-0-review.md) | `8295210dd020519c2d5467470da667c47ec764fa766fb03a6b74b753244d2350` |
| [task-1-report.md](task-1-report.md) | `455db233f5a623576fdb3d2877e227b690a91186f5e393cd9b9e535a0dd242bf` |
| [task-1-review.md](task-1-review.md) | `0117a857b0d4b3e24517848ba54970d3677bf729aace39750fef5030a29f3c04` |
| [task-2-report.md](task-2-report.md) | `a21a7f0c905d6e455439a5f0e225ef26e32a2529327b226a168de083ae2db3e3` |
| [task-2-review.md](task-2-review.md) | `b108d68f92316e547872b70231e29481b3149c2823b3ac55dbf00fbb5c6bce5a` |
| [task-4-report.md](task-4-report.md) | `ee876123e0fea69bfdf763eb6a0ea118ed9c27f652a03003714b2765cc2e8ca4` |
| [task-4-review.md](task-4-review.md) | `5706863ad4338d6b3b7fa09e01935fa7370dc77fd9b7a252e0e7fcbddbe443a7` |
| [task-6-report.md](task-6-report.md) | `096604ae9ea849e02bd78a97390bd1308b99ce21eafeb88292f530547d4aa9fa` |
| [task-6-review.md](task-6-review.md) | `22d8f37e8df9f071ca4fe2cf0f8e7f9bae8ac3e98661466bd9a5ecfa266860f9` |
| [task-7-report.md](task-7-report.md) | `0dbc63958cef5720c0656273e2dcabdabcca84d5e313f6a522e74b1c2eb6d83c` |
| [task-7-review.md](task-7-review.md) | `a08071eead8bfb60812d61163c40e90b39fa52dcb3a530fc16035706014745d5` |
| [task-8-report.md](task-8-report.md) | `36548704deb8bb6449ff17f4951dca1bea7268871c2b3f618c1f3e5530e937d8` |
| [task-8-review.md](task-8-review.md) | `56334abc86df9baffeb2b20d2084c86af07aca6d2dce2a99f087ff31a790ab17` |
| [task-9-report.md](task-9-report.md) | `b6fe662123207e9f3c2a61b37337ea08a7bcf9dc3b11ed509056ac93618fd43d` |
| [task-9-review.md](task-9-review.md) | `e5d0092639b2019d3bed531e87893a69732160183a7609985c8317adcb78b7b5` |
| [task-10-report.md](task-10-report.md) | `b16db3d303048a1ffdb98257d3763a9521e1ba48b8574ff2a839a855de9c5eb0` |
| [task-10-review.md](task-10-review.md) | `30afe5026c0ec49fd979eb166655fb31121939311a42c945459096b90339e073` |
| [task-11-report.md](task-11-report.md) | `c3787645db8fef7fe33a8f4dbf199b3f65927e25f8ff3bed966052c4e4575f2a` |
| [task-11-review.md](task-11-review.md) | `410e2d8efad329aa664f922fcc0aa21312bda571c23a0fa045f77f92145ee149` |
| [task-12-report.md](task-12-report.md) | `34606968991ab37a70a256d000a07bd5c1fcaf1ab6dbbc3d6ea2621ca51579f8` |
| [task-12-review.md](task-12-review.md) | `f27b992b27303481f03db5e7c947fcab0412fa88fa5609cfd87e356de7aa9457` |

| [task-3a-report.md](task-3a-report.md) | `f7fe9c33a14f211d0910a3b9f0a026e6c957805497744b3b7e09cb2213094c50` |
| [task-3a-review.md](task-3a-review.md) | `c8f80361754a7deba7bfa560893586706d1514b74977984ea917e571b2d1a558` |
| [task-3b-report.md](task-3b-report.md) | `c0ca378159ce34827474ea581a79d9059442f5cedc1ddf85169ed0c021bf13fd` |
| [task-3b-review.md](task-3b-review.md) | `dcf60e47e2482797e4371e63cfc867934ad056cb3c47ede6c104cceecbcf97ef` |
| [task-5a-report.md](task-5a-report.md) | `ff5a417f3c7d4eb5faafef4f9d894af653e0917272c5ffa27c61faec2a18dd5d` |
| [task-5a-review.md](task-5a-review.md) | `67ef6e08cf9cb0dfbc8f0f02e8992ea47df368d1699d648962cb9f133e48bcc1` |
| [task-5b-report.md](task-5b-report.md) | `061a7aac71fed8fe653f383c2e4c880f5dde4b5ebf9a4ef5b456e4d18ed229a5` |
| [task-5b-review.md](task-5b-review.md) | `8ea660a566379ec5a360ff736bd45c67ff41def89f4b0d8979522c2ef4ebbb89` |

Task10原DONE_WITH_CONCERNS及双环境成本保留。Task12漏接typed研究、实际验证替代直接seed可达性、
DB stop/start换端点失败、首轮98失败与第二完整门禁、I1真实actor修复等裁定理由和验证成本全部在
上述原报告/ledger中；未知token/人工工时不写0。控制者将在最终门禁后追加Task13和最终review裁定，
本文件没有预写通过或清理其他plan目录的授权。
