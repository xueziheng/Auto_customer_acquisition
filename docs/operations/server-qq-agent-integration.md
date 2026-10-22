# 服务器 QQ 与 Agent 升级整合

> 2026-10-23更新：下文保留各阶段历史记录；最终服务器切换、配置保留和测试结论以[最终验收报告](server-qq-acceptance-20261023.md)及[逐来源结果](server-qq-source-results-20261023.md)为准。早期“未部署/未操作配置/未联网”描述仅适用于当时阶段。

本次源码基线为 QQ `5b6474a`，第一父分支文档提交 `658d126`，整合输入为昨日升级 `682c541`。当前服务器线上源码备份为独立 `dd023e4`。这三份源码不包含运行数据库、邮箱、真实凭证、对象存储数据；不能仅从 GitHub 恢复完整业务环境。配置协议和引用保留，未读取、复制或改变真实配置值。

## 保留矩阵

| 能力 | 整合方式与边界 | 证据 |
|---|---|---|
| 企业资料、Obsidian、平台模型 API | 原权限/配置协议；平台管理者配置，模型不接触凭证 | unit/evals、Web；真实配置未操作 |
| 已确认产品、地区、采购意图 | 升级 InventoryPanel → acquisition job → 持久准备队列；保留原来源协议 | customer_preparation_runtime 的 DeepSeek、自动准备和双企业链 |
| 获客来源注册与设置 | 保留 SourceDefinition、平台注册及11项设置协议；不是54个已联网来源 | [逐来源表](server-qq-source-capabilities.md)；unit/evals覆盖来源与平台设置 |
| QQ inquiry / reply | 13个独有实现及对应测试保留；显式 CLI profile/scope、最长24h验收授权；没有装入普通 scheduler 自动启用 | test_business_inquiry、test_business_reply |
| 普通业务邮件 UI | 合并保存草稿、未知结果原请求恢复、原QQ负责人按钮、模型提取原话与来源记录；不把草稿当已发邮件 | Web 两套followup测试13通过 |
| 正式普通对话 | 原Campaign明确授权、已验证联系人、本人邮箱/当前owner、逐次Gateway检查；测试scope不替代正式授权 | ordinary_dialogue_factory |
| 同原件去重 | 正式准入在原件锁内拒绝QQ询盘历史；QQ扫描排除正式binding，领取/发送前再检查binding | factory新增 inquiry_claim 与 inquiry 反向领取断言 |
| 迁移 | 保留QQ0086的SQL/触发器/RLS语义，仅修复import空行；新增009x沿单链，head0102 | 历史0086往返/非空回退保护、当前head整库迁移 |
| 服务套餐 | 保留升级可复用实现；没有写入活动套餐、改真实额度或启动任务 | unit/evals与准备链；生产配置未验 |

## 冲突裁定

忽略CRLF比较得到197项不同、26项QQ独有、227项升级独有。126个Git冲突以 `7715b40` 导入快照为语义参考：121项可由“QQ与导入快照一致→升级增量”、两边相同或单边增量规则归并，5项需人工组合。没有对整库选择 ours/theirs。另检查所有非冲突差异，纠正一项自动合并混入的旧 model-settings 测试。

人工组合包括模型followup与ordinary_dialogue能力并存、QQ询盘原话展示与升级草稿/恢复功能并存、BusinessMail新增正式普通对话入口。QQ与升级Web测试各自保留完整fixture，避免两套helpers误混。0086测试先核对历史表，再在当前head准备数据，退到0086以原始SQL核对不可变历史；不让新版ORM读取旧schema列。

QQ发送去重helper改用五个只读头字段Protocol，使原线程回复与冷邮件保持不同业务契约。没有真实收发邮件或调用Provider。

## 本地验证

解释器使用已有Python3.12依赖环境，但所有源码、pytest根目录及OpenAPI均来自本整合checkout。Web lock与依赖树相同，复用ignored dependency-only node_modules。

- QQ基线unit：40通过；整合QQ询盘/回复/业务邮件unit：106通过。
- `python -m pytest tests/unit tests/evals -q --basetemp=/tmp/tradeos-qq-unit`：10086通过，76跳过。首次长macOS临时路径触发AF_UNIX路径限制；短临时目录重跑通过，未修改测试规避断言。
- `npm test`：65文件、694用例通过。
- `npm run build`：通过，含vue-tsc；Vite原有动态/静态导入警告。
- `npm run lint`：0错误，1213警告；未进行全库样式重排。
- `npm run gen:api`：生成两份schema成功，与已暂存生成文件一致。
- `python -m mypy domains shared tool_gateway connectors`：458文件通过。
- 扩展968文件mypy检查发现5个类型错误：QQ business_reply 4处工厂/registry契约，knowledge.py的ModelProvider缺prepare静态契约1处。尚未修复，不宣称全层类型检查通过。
- Agent组合PG：37通过/218.16秒，包含 DeepSeek搜索/公开页/私有适配/分配，准备→QQscheduler首封MIME→回复→普通续聊，以及两企业隔离链；外部模型、搜索、邮箱端口为受控替身，数据库、Gateway、队列与工厂为真实实现。
- QQ初轮PG：37通过、1失败，失败为历史schema测试使用新版ORM；该测试修复后在Agent组合PG通过。最终QQ+普通对话组合仍在运行，结果追加于下方。

真实搜索来源、真实模型成本、真实邮箱、生产额度、生产迁移和完整服务器业务恢复均未实测。没有把54项登记数、合成测试或局部通过计数当作真实贸易机会产出。用户后续要求先同步服务器再测试，由控制者执行；本任务不部署、不推送或操作服务器。

最终QQ询盘/回复/业务邮件跟进、历史迁移和普通对话工厂组合：50通过/122.73秒。新增同原件两种顺序回归均通过；QQ先领取时准入403且零binding/模型/发送，正式准入在先时QQ扫描不再发现且迟到领取被拒绝。原件行锁内的双向检查覆盖两个入口先后提交；没有单独运行并发压力测试。`alembic heads`确认为单head0102。最终结构自检及ruff全部通过。

## 服务器最终修复

用户要求后续实现和测试移至服务器。本节全部测试来自独立服务器checkout `/srv/tradeos/acceptance/qq-20261019/agent-upgrade-20261022/src`，起点 `e8fa19f`；与上文历史本地测试分开计数。没有修改运行配置、运行数据库、凭证或服务状态。

- 恢复QQ跟进面板30秒只读轮询；有身份、无进行中请求、无dirty草稿/说明/状态、无未知请求时才刷新。身份撤销、403及卸载清理timer，迟到响应由原gate丢弃。
- 新增9个timer回归，原实现首轮3失败/5通过明确复现没有第二次GET；修复后跟进三个文件22通过，覆盖后台发送/原话更新、四类编辑保护、未知写入、读取不重叠、身份撤销迟到响应、权限撤销及卸载。
- 五项静态契约问题已在服务器闭合：三个SQLAlchemy UoW将仓储字段注解为已有Repository Protocol，具名工厂有显式类型；单handler注册适配现有Pipeline窄结构端口；模型工厂返回实际拥有prepare的DeepSeekClient（兼容客户端为其子类）。不加Any或type-ignore，不改变发送/模型授权。
- expanded mypy：968文件通过；定向Python17通过；Web typecheck通过。结构检查发现一次apps导入域私有repository的新问题，随后移除该import，工厂使用已导入的具体adapter返回类型。

最终完整回归、QQ数据库备份/迁移及运行切换由控制者执行，本修复不提前宣称这些动作完成。服务器早期缺少SQLAlchemy等依赖，import-not-found属于环境准备阶段，完整依赖就绪后重新核验。
