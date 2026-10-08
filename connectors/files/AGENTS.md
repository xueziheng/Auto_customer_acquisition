# connectors/files/ —— 文件接入

继承 connectors 与根总纲。员工原件必须进入不可变 artifact_store；解析、转录及分析是独立产物，必须保留原件关联，不能替换原件。连接器不判断企业授权、不直接写业务表。

## 企业知识文件离线解析

`knowledge.py` 与固定 `_knowledge_parser.py` 只接收受信上游已授权的 bytes，不接受用户任意文件路径。支持 UTF-8（可 BOM）TXT/Markdown/CSV、DOCX、XLSX、PDF，以及 PNG/JPEG/WebP。

- 原件最多10 MiB；文字最多60000字符、PDF最多100页、进程最多30秒。超限拒绝，不静默截断。
- Office ZIP 限条目数、解压总量/单条目/压缩比，拒绝加密、宏、路径注入与重复条目。XML 禁 DTD/entity。外链不访问；公式不执行，原始单元格地址保留并显式警告。
- PDF 使用独立 knowledge-text-v1 契约：CPython3.12.x + pypdf6.16.2，不借用 evidence_text 的3.12.14固定 profile 验收。保留全部已提取文字及原页号。
- PNG/JPEG/WebP 使用 Pillow12.3.0 真解码，拒绝动画和超过2000万像素的原件，去元数据后归一为PNG。最长边1600；缩放显式提示，其清晰度不保证小字准确识别。
- PDF 无文字页或含图片页由固定系统 pdftoppm 离线渲染。最多10个视觉页；每图8 MiB、合计16 MiB。超限固定 source_limit_exceeded，不挑选前10页冒充完整。纯文本PDF仍可100页。
- image_source_pages 保存图像数组序号对应的原PDF页；独立图片使用第1页。映射来自解析器，不能信任图像自带页号。
- 离线解析本身不调用模型OCR。受信上游用 ModelInputImage 经统一模型网关处理视觉资料；视觉转录明确标为待人工核验，无字照片只能形成图像推断，不能造事实。

## 隔离、资源与输出

解析在 bwrap 隔离 PID/网络/文件系统中执行，只挂系统运行库、固定程序、必要的PDF/图像库和字体。凭证、宿主home和业务存储不可见。CPU/内存/输入输出有界，不执行上传文件代码。进程创建及清理阶段的二次取消延后传播，超时/取消杀独立进程组并等待回收，无无沙箱降级。

warnings 最多4个，父进程验证固定白名单，不含原文、路径或异常，独立存入 parse_warnings，不污染 source_text。图片 bytes 不进 repr、日志或普通DTO投影；输出长度、哈希、magic与页映射再次验证。解析错误只携带固定 reason。

Sources 只保存安全纯文本原文或固定原件说明；模型转录应放带明确标签的 Docs，不得冒充原文。
