# workflows/employee_work_intake/ —— 员工上传处理流程（Phase 1 浅）

## 触发

员工上传批次落 artifact_store 后。

## 主要状态

```text
preprocess（files connector：类型识别、OCR/转写）
→ extract（team_operations 能力：客户识别、需求、承诺、进展）
→ wait_confirmation（WAITING_HUMAN：员工确认或修改提取结果）
→ apply（确认版本落各域：need 字段、commitment、进展备注）→ complete
```

## 关键约束

提取链条完整保留（原始→提取→修改→确认，不覆盖）；未确认的提取不触发任何提醒或状态推进；apply 步骤逐条幂等。
