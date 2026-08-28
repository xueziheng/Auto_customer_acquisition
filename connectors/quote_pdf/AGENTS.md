# connectors/quote_pdf/ —— 离线客户报价 PDF 渲染器

## 职责

将 shared 唯一的 `CustomerQuoteView` 渲染为确定性 PDF bytes。只使用固定的本地
ReportLab 字体与内存输出，不访问网络、密钥、数据库、ArtifactStore 或客户文件路径。

## 边界

- 不导入 `domains.*`；不解析金额、不验证商业批准、不读取报价内部依据。
- 不注册 Gateway、ConnectorRegistry 或生产装配；`bytes` 不代表生成、下载、发送获授权。
- 配置的字节、页数及文本上限必须显式提供且拒绝非法值；不设置生产默认值。
- 错误使用 shared 的 `QuotePdfRenderError`，不得暴露客户文字、字体路径或原始异常。

## 依赖

允许 `shared.*`、`connectors.base`、固定 ReportLab/pypdf 运行时；禁止外部服务 SDK、
网络、系统字体 fallback 和用户提供的文件路径。
