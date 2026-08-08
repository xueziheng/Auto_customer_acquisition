# connectors/files/ —— 文件接入（Phase 1）

## 能力范围

员工上传文件（截图、PDF、Excel、Word、语音）的接收与预处理：类型识别、大小校验、转交 artifact_store。语音转写、OCR 的调用入口也在这里（转写结果与原件都存）。

## 铁律

原件必进 artifact_store（不可变）；解析产物（转写文本、表格提取）作为独立 artifact 关联原件，不替代原件。
