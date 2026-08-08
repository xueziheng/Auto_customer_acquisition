# team_operations/ —— 员工工作提取

## 职责

处理员工上传（微信/WhatsApp 截图、聊天文字、邮件、PDF、Excel、语音转写）：识别客户、关联商机、提取需求字段、提取双方承诺、判断商机是否推进、生成结构化结果供员工确认。

## 关键纪律

- 原始文件先进 artifact_store，提取链条完整保留（原始 → Agent 版 → 员工改 → 确认版），**不覆盖**
- 承诺提取时相对时间用消息时间戳 + 客户时区解析为绝对时间；没把握标 due_at_uncertain（commitments 域规则）
- 提取结果一律「待员工确认」状态，未确认不触发提醒和状态推进
- 老板问「张三今天做了什么」的答案来自确认后的结构化记录 + 原始证据链接，不是模型现场编纂

## 输入 / 输出

输入：上传批次（artifact 引用）、员工与客户上下文。
输出：ChangeSet（extract_facts / create_commitment / update_need_fields / progress_note 条目）。
