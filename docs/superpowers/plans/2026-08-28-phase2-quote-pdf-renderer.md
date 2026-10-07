# Task 7：确定性离线客户报价PDF Implementation Plan（已自检，待前置交付）

> **For agentic workers:** REQUIRED SUB-SKILLS: Use superpowers:test-driven-development and superpowers:verification-before-completion。此任务由主计划的SDD控制器派发，不另启动executing-plans批次或子代理。只在前置契约核定后实施。三个TDD切片自检提交，完整Task7统一独立审查，不提前开始T8/T10。

**Goal:** 将真实报价的唯一客户白名单投影渲染成确定性、资源有界、无外部动作/附件的PDF bytes。
**Architecture:** quotations定义Renderer Protocol及金额投影验证；connector结构化实现，不导入domains。shared只承载唯一客户DTO、模板版本和跨层固定渲染错误；授权/存储/Gateway归T8。
**Tech Stack:** Python 3.12+、runtime固定reportlab==5.0.1及pypdf==6.16.2；无网络/OCR/新框架。
**Spec:** `docs/superpowers/specs/2026-08-28-phase2-costing-quotation-design.md` §7；主计划Task7、`2026-08-28-phase2-quotation-versions.md`客户投影及`2026-08-28-phase2-quote-files.md`模板契约。派发时另附实际接口与PDF验收交接，官方依赖核验已由控制器完成，不重复联网选型。

## 0. 前置与精确文件

工作树`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/phase2-costing-quotation`。先读根AGENTS/HANDBOOK、connectors/shared/domains/quotations/tests就近AGENTS；不把机检允许误作目录许可。
T4/T6是前置交付，不能把brief当现有实现。控制器先将context中的validate_customer_projection补充前移T4并核验真实接口；T7不修改T4 brief，也不在renderer补金额规则。
首次真正PDF作者命令（包含运行会实际render的pytest）之前，由控制器依handoff执行已核定marker：其Node/脚本路径、create、expected-output-count=1、output-format=pdf，成功恰一次并记录。未完成不得开始PDF作者命令；本brief不重读或解释PDF技能。
T7测试在项目tradeos-py312环境；bundled pdfinfo/pdftoppm是handoff定位的T10工具，不替换项目Python。本文是计划而非渲染验收；实施时按5.1准备锁定依赖，首次会创建PDF的命令前通知控制器运行marker，未收到成功确认不得开始作者命令。

| 文件 | 责任 |
| --- | --- |
| Create `connectors/quote_pdf/AGENTS.md`、`__init__.py`、`client.py`、`manifest.py` | 离线renderer、受信本地字体、插件元数据与固定边界 |
| Create `connectors/quote_pdf/layout.py`、`limits.py` | 单用途排版/页数与输出限制；避免client膨胀，不建通用渲染框架 |
| Modify `domains/quotations/service.py` | 定义QuotePdfRenderer Protocol；重导出同一shared错误 |
| Modify `shared/schemas/quote_document.py` | 仅追加固定QuotePdfRenderError/Code，原CustomerQuoteView形状/hash不改 |
| Modify `pyproject.toml` | runtime pin两依赖，若旧dev重复声明则去重；不新增font/network依赖 |
| Create `tests/unit/test_quote_pdf_renderer.py` | 真实ReportLab/pypdf、合法域投影/篡改拒绝、界限/字体/安全对象/确定性 |
| Modify `docs/adr/0019-quote-pdf-artifacts.md` | Protocol与shared错误归属、呈现边界/资源承诺；不放宽九条 |

本切片仅测试同时导入域公开投影和connector；connector不import Protocol所在domains，也不读cost/basis/Need/ArtifactStore。不改ConnectorRegistry、Gateway核心、生产注册或任何数据库表。

## 1. 一次定义公开契约

```python
# domains/quotations/service.py；结构化Protocol，不要求connector继承/import它
class QuotePdfRenderer(Protocol):
    def render(self, view: CustomerQuoteView, *, template_version: str) -> bytes: ...
# connectors/quote_pdf/client.py
class ReportLabQuotePdfRenderer:
    manifest: ConnectorManifest
    def __init__(self, maximum_bytes: int, maximum_pages: int,
                 *, maximum_text_bytes: int) -> None: ...
    def render(self, view: CustomerQuoteView, *, template_version: str) -> bytes: ...
    async def configure(self, secret_resolver: object) -> None: ...
    async def health_check(self) -> bool: ...
```

CustomerQuoteView仅从shared.schemas.quote_document导入，是T4的同一个class；模板集合仅从shared.schemas.quote_files.QUOTE_PDF_TEMPLATE_VERSIONS导入，现为quote_pdf_v1。不得复制DTO/模板集合或把template作为自由路径/HTML文件名。
三个limit均必须配置、正int且排bool；缺参不构造能力，非法值固定invalid_config。不设生产默认值，不从PDF大小猜客户文本上限；最大页数/bytes/文本配置归T8。
render只接受该DTO实例（不是dict/内部QuoteDetailView）；返回非空PDF bytes，不接actor、approved、path、URL、tenant凭证或金额计算选项。接受DTO不表示该报价获准使用；正式来源链由域/T8控制。
configure为无副作用no-op，不解析secret_resolver；health_check只检查已配置限额和受信本地字体/依赖可用，返回bool，不生成PDF/访问网络/读密钥。异常日志不打印路径、客户文字或原异常。
manifest沿connectors/base.py现有ConnectorManifest：connector_id='quote_pdf'、capabilities=('quotation.pdf.render',)、secret_refs=()；rate_limit_note说明上限/Gateway由部署配置，compliance_note说明离线/无授权/无发送。client.manifest=MANIFEST；__init__仅显式导出client，manifest文件导出MANIFEST。不为此实现现有骨架Registry。

shared/quote_document.py追加：

```text
QuotePdfRenderErrorCode = Literal['invalid_config','invalid_input','template_unsupported',
 'text_limit_exceeded','page_limit_exceeded','byte_limit_exceeded',
 'font_unavailable','unsupported_glyph','layout_failed','render_failed']
QuotePdfRenderError(ConnectorError): code:QuotePdfRenderErrorCode;is_retryable=False
 __init__(code:QuotePdfRenderErrorCode)  # 不接自由消息/context/原文
```

继承shared.errors.ConnectorError；中文消息固定依次为“PDF配置无效/客户视图无效/模板未注册/客户文本超限/PDF页数超限/PDF字节超限/PDF字体不可用/字体不支持客户字符/PDF排版失败/PDF渲染失败”。未知code不能成为任意错误消息。quotations.service重导出同class，connector只import shared错误。
预期字体/排版/大小错误各映射对应code；意外ReportLab异常统一render_failed from None，不泄文本/路径/栈内请求。取消/KeyboardInterrupt/SystemExit不吞成普通业务错误；无自动重试/换模板/降字号或替换字符。

## 2. 来源和金额边界

正式输入唯一链：真实QuoteDetailView → 域project_customer → 域validate_customer_projection(view,quote) → T8当前正式授权链 → renderer。T7只测试这些纯域函数，不实现当前授权/批准或读取DB。
T4已定义金额按line.rounding输出、数量整数十进制、有效期UTC、terms原序；新validate_customer_projection逐字段比对同一真实quote的project_customer结果，任何差异抛T4 QuotationError('basis_mismatch')。它不重算利润、不引入金额正则/新舍入规则，也不作为授权。
renderer不parse/量化Decimal，不计算单价×数量，不校正币种、位数、指数或符号；其文本/字体检查只是技术安全检查。不得把“renderer没有拒绝某金额字符串”解作域已认可；HTTP不得直接把客户端CustomerQuoteView交render。
T7用T4已验证构造工厂/fixture产生合法QuoteDetailView再投影；篡改unit_price_display/total_display/currency/quantity_display需由域函数在render前拒绝，不要求connector读内部报价。

## 3. 排版、字体及严格资源顺序

固定A4、本地Vera字体、36pt页边距、正文10pt/14pt行距、标题14pt；几何是排版常量，不是商业阈值。页脚“Page n”，不为总页数另做无限预渲染；字体不可自动缩小到不可读来逃避限制。
完整展示：本公司name/address/contact、客户account_name、quote_id/version、description/specification/unit/quantity_display、unit_price_display/total_display/currency、valid_until_display、按原序全部approved_terms。短价格区可用表格，长规格/条款必须使用可分页flowable，不将整个长文塞不可分割表格单元。
顺序必须是：校验DTO/模板/配置 → 限制全部客户文本 → 加载字体并核覆盖 → 转义构造story → 受限逐页build → 受限PDF输出 → 返回bytes；拒绝不得给出截断的“成功PDF”。
maximum_text_bytes计算所有实际渲染客户字符串及str(version)的UTF-8字节合计，approved_terms每项都计，before story拒超限。逐字段累计，先用len(text)>剩余额度短路，再有界encode，不能先join/escape巨型输入；非法Unicode编码固定invalid_input。空字符串不创建flowable，非空项至少消耗一byte，避免零长度terms制造无限story。
原值不trim/改Unicode/换ASCII标点，不截断。文本先html.escape再交解析flowable，不接原始HTML/RML；换行显式呈现，连续空白用可保留空白的flowable（如适当包装XPreformatted）处理，长单词确定性换行或明确layout_failed，不越界裁切。不能调用图像/链接/文件加载接口；不支持的控制字符固定invalid_input而非删掉。
所有文本（含标题/页脚数字、每个客户字段/terms）使用所选字体对应的实际TTFont字形映射检查：codepoint必须在该font.face.charToGlyph且glyph非0；不能只检查TTF文件存在、Unicode可编码或charWidths有fallback。只将明确的排版换行从glyph检查分离，不能跳过可见字符。
Vera.ttf/VeraBd.ttf只从固定已安装ReportLab包fonts目录解析，不能接用户路径或系统字体fallback；正文/标题分别检查其实际字体。无需凭记忆声称Vera覆盖中文/emoji；缺字固定unsupported_glyph，字体文件缺失/损坏为font_unavailable，不画方框/转写/替换商业文字。
字体注册使用固定内部名称，检测重复注册的字体身份，不依赖全局font注册顺序改变输出；每个document独立字体子集状态。实施时核验所pin ReportLab实际API并加针对测试，不新造远程字体探测器。
maximum_pages在每页开始、绘制本页内容之前检查；超过N在第N+1页开始中止，不能等无限build完成后才len(reader.pages)。最终页数检查仅防御性复核；长表/flowable无法分割须layout_failed。
maximum_bytes由有界BytesIO写入器检查seek/write后的最终位置/最大已写extent，超限立即byte_limit_exceeded；最终getvalue长度再核。ReportLab可能在向sink写入前内部序列化PDF：这保证输出/缓冲上限，**不是硬RSS沙箱**；输入文本及页数也有先验上限，不宣称单靠len(bytes)保护内存。
不新增进程/网络/通用资源框架；超限错误不自动调大配置。T8是否进一步隔离执行是后续职责，本任务不把普通线程timeout冒充内存隔离。

## 4. 确定性与真实PDF对象安全

Canvas显式invariant=1，固定页面、字体/模板、压缩设置、metadata常量（如固定title/author/creator），不写当前时间、run_id、随机UUID或内部对象repr。不通过事后修改已存PDF“修复”确定性。
同view+template在相同受信runtime/fonts产生相同bytes；限制值不进入PDF，只决定准入。测试同实例、不同实例、插入别的文档渲染之后、独立新Python进程均一致，不能只测对象复用缓存。
输出不添加annotations、links、outline、attachments、AcroForm、JavaScript或actions；用户文本包含URL/XML仅是原文本。PDF名/metadata不含内部供应商/成本/利润/来源引用。
安全测试必须实际解析pypdf对象图：从trailer['/Root']解引用IndirectObject并递归DictionaryObject/ArrayObject（visited避免环）；检查Catalog、每页、Name树及相关字典，不依赖str(reader.trailer)或IndirectObject repr。
明确拒绝Catalog /OpenAction、/AA、/AcroForm、/Names中的/JavaScript或/EmbeddedFiles；任何对象的/A、/AA、/OpenAction、页面/对象的/Annots、/AF均拒绝。检查键与/Type、/S名称值：/Type为/Action、/EmbeddedFile、/Filespec、/RichMedia拒绝；/S为/JavaScript、/Launch、/URI、/GoTo、/GoToR、/Named、/SubmitForm、/ImportData等动作也拒绝，不能仅查字典键。测试reader.attachments为空、outline为空；renderer不产出任意动作类型。
正文extract_text、metadata文本/字符串、outline、解引用的字符串及相关stream中扫描独特内部sentinel。扫描内部标记只是泄露测试，不把它写成业务内容过滤规则；合法客户文本不会因出现“margin”等普通词被renderer擅删。
PDF结构安全＋提取正确不代表布局正确；真实逐页PNG/视觉检查按handoff留T10，不在T7声称完成视觉验收。

## 5. TDD切片/提交

### 5.1 契约与域边界

- [ ] RED新增renderer Protocol结构化一致、shared错误同对象、manifest无secret、缺/非法上限、模板拒绝、真实合法域投影及篡改拒绝测试；若测试会实际创建PDF，必须此前完成marker。仅只读依赖/契约检查不提前执行marker。

```python
def test_domain_rejects_changed_customer_price(quote_detail):
    view = project_customer(quote_detail)
    changed = view.model_copy(update={'total_display':'tampered'})
    with pytest.raises(QuotationError) as caught:
        validate_customer_projection(changed, quote_detail)
    assert caught.value.code == 'basis_mismatch'
```

- [ ] 在项目环境按已批准pins准备依赖（如缺失，按项目既有安装方式；不再选版本/联网研究）。RED：`python3 -m pytest tests/unit/test_quote_pdf_renderer.py -q`，失败必须实际到目标契约/行为，不能拿ImportError当业务RED。
- [ ] GREEN仅新增本任务Protocol/shared错误/connector目录与manifest、构造配置检查；T4验证函数必须已由控制器前移交付，若未交付只报前置缺口，不由renderer临时替代。
- [ ] 提交`feat: 定义离线报价PDF渲染契约与固定错误`。

### 5.2 真实字体、资源和全字段渲染

- [ ] RED实际ReportLab+pypdf：真实客户字段全部可提取、terms不漏/顺序不变、长规格换页、UTF-8中文/emoji文本超限在story/font前拒绝、正确limit边界+1拒绝、恶意HTML/URL只作文字或固定失败。

```python
def test_pdf_contains_domain_projected_total(renderer, customer_view):
    content = renderer.render(customer_view, template_version='quote_pdf_v1')
    reader = PdfReader(BytesIO(content))
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    assert customer_view.total_display in text
    assert customer_view.quote_id in text
    assert not reader.is_encrypted
```

- [ ] RED字体：实际读取Vera映射选有glyph的非ASCII字符与无glyph字符分别成功/unsupported_glyph；故意缺文件font_unavailable；不能mock stringWidth成功替代字形检查。测试原弯引号/连字符/换行不被静默ASCII改写。
- [ ] RED页限制用真实长文迫使第N+1页并监测页钩子中止；byte边界用同输入先取得确定长度L，在L成功、L-1固定失败；text UTF-8累计含全部terms，空串不生成额外flowables。异常无客户原文/字体路径，不能返回部分bytes。
- [ ] GREEN在layout.py实现固定模板与可分页flowables、在limits.py实现文本预检/页钩子/有界buffer；client只编排。禁止网络socket/HTTP，固定本地字体外不得file/image加载；不引入金额转换。
- [ ] GREEN同目标命令，提交`feat: 实现受限字体与确定性客户报价PDF排版`。

### 5.3 确定性、对象图安全与回归

- [ ] RED同view/template重复/新实例/先渲染其他字符集/新进程bytes严格相等，变更任一商业展示字段或受信模板不应错误复用旧bytes。仅quote_pdf_v1，不构造假v2来绕注册。
- [ ] RED实际Catalog/页面/对象树/附件/outline检查；给测试检测器单独制作带间接/嵌套动作和附件的受控反例，确保它会失败，不只在安全输出上空跑。反例只内存构造，不访问URL或执行动作。
- [ ] RED在合法内部quote basis/source放独特sentinel、经真实project_customer渲染后正文/metadata/对象流无泄露；恶意客户`<img src=...>`/`<a href=...>`不得形成PDF链接/附件，网络调用次数为零。
- [ ] GREEN最小修复确定性/对象安全，不能靠删除客户文字、PDF后处理遮盖泄露、mock renderer或禁用断言通过。
- [ ] 运行`python3 -m pytest tests/unit/test_quote_pdf_renderer.py tests/unit/test_quotation_contracts.py -q`与`python3 scripts/check_boundaries.py`，记录marker/RED/GREEN/未运行T8/T10；提交`test: 验证离线PDF对象安全与跨实例确定性`，ADR/AGENTS在本提交同步。

## 6. 交接

T7不碰ArtifactStore/DB/审批/actor/HTTP/Gateway生产装配；renderer返回bytes不授予生成/下载/发送许可。T8调用真实域投影验证及正式授权、按T6稳定run/key存储；历史只读仍不能重生成。
T10使用handoff已定位的pdfinfo/pdftoppm及逐页视觉流程。最终用户PDF产物展示遵控制器handoff，不把测试PDF或页图当本任务最终产物；源码/文档按普通绝对文件链接交付。
