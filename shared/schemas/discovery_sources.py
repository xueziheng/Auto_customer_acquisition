"""跨 API 与配置存储共享的获客来源目录；不包含凭证或生产就绪结论。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CredentialField(BaseModel):
    """声明管理界面允许填写的凭证字段名称，不承载字段值。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str


class DiscoverySourceDefinition(BaseModel):
    """来源静态说明；接口存在、凭证已保存与运行可用分别判断。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    selection: Literal["selected", "backup", "example"] = "selected"
    category: str
    website_url: str | None
    registration_url: str | None = None
    docs_url: str | None = None
    access_methods: tuple[str, ...]
    coverage: str
    description: str
    credential_mode: Literal[
        "public", "api_key", "oauth_client", "access_token", "commercial_pending",
        "managed_session", "authentication_pending",
    ] = "public"
    credential_fields: tuple[CredentialField, ...] = ()
    integration_status: Literal["available", "partial", "planned"] = "planned"
    integration_note: str = "采集待接入；配置凭证不代表接口权限或运行可用。"


_API_KEY = (CredentialField(name="api_key", label="API key"),)
_PAGE_METHODS = ("网页搜索", "公开网页读取", "受控浏览器（待接入）")
_PAGE_NOTE = "已有通用搜索和公开页面读取；逐站持续采集及浏览器运行待接入或验收。"
_COMMERCIAL_NOTE = "需商务确认 API 权限、国家、字段和认证方式；凭证字段与采集均待接入。"


# 官网、文档和能力范围对应主清单；新增或移除来源时同步更新该文件。
# docs/operations/customer-discovery-source-expansion.md（2026-10-11）
DISCOVERY_SOURCES: tuple[DiscoverySourceDefinition, ...] = (
    DiscoverySourceDefinition(
        source_id="hunter", name="Hunter", category="企业与联系人",
        website_url="https://hunter.io/", registration_url="https://hunter.io/api-keys",
        docs_url="https://hunter.io/api-documentation/v2", access_methods=("官方 API",),
        coverage="多国企业及企业域名邮箱，覆盖以账号和返回结果为准",
        description="Discover 找公司；Domain Search / Finder 补全邮箱；Verifier 验证邮箱。",
        credential_mode="api_key", credential_fields=_API_KEY, integration_status="partial",
        integration_note="账户验证、域名邮箱查询和邮箱验证已有实现；Discover / Finder 待接入。运行仍需就绪检查。",
    ),
    DiscoverySourceDefinition(
        source_id="apollo", name="Apollo", category="企业与联系人",
        website_url="https://www.apollo.io/", registration_url="https://www.apollo.io/sign-up",
        docs_url="https://docs.apollo.io/docs/api-overview", access_methods=("官方 API",),
        coverage="多国企业、职位与联系人，依套餐权限",
        description="企业和人员检索；People Search 不返回邮箱，取得邮箱需相应补全权限。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="pdl", name="People Data Labs", category="企业与联系人",
        website_url="https://www.peopledatalabs.com/",
        registration_url="https://dashboard.peopledatalabs.com/",
        docs_url="https://docs.peopledatalabs.com/docs/company-search-api",
        access_methods=("官方 API",), coverage="多国企业与人员资料，依字段授权",
        description="Company Search 补企业；Person API 补工作联系方式。免费测试不提供可用邮箱。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="snov", name="Snov.io", category="企业与联系人",
        website_url="https://snov.io/", registration_url="https://app.snov.io/register",
        docs_url="https://snov.io/knowledgebase/how-to-get-api-credentials/",
        access_methods=("官方 API", "异步任务查询"), coverage="企业与业务邮箱，依套餐权限",
        description="域名邮箱、人员补全与验证；部分接口异步返回，API 权限须单独确认。",
        credential_mode="oauth_client", credential_fields=(
            CredentialField(name="client_id", label="API User ID / client_id"),
            CredentialField(name="client_secret", label="API Secret / client_secret"),
        ),
    ),
    DiscoverySourceDefinition(
        source_id="ocp", name="OCP Data Registry", category="全球采购数据",
        website_url="https://data.open-contracting.org/",
        docs_url="https://data.open-contracting.org/",
        access_methods=("OCDS JSON 下载", "CSV / Excel 下载"), coverage="多国、多发布机构",
        description="采购事项、采购单位、中标企业和合同数据；逐数据集确认更新范围，与国家原站去重。",
    ),
    DiscoverySourceDefinition(
        source_id="ted", name="TED", category="欧洲采购公告",
        website_url="https://ted.europa.eu/",
        docs_url="https://docs.ted.europa.eu/api/latest/search.html",
        access_methods=("公开 Search API", "XML 批量下载"), coverage="欧洲公共采购",
        description="已发布公告、采购单位、期限与授标信息；公开查询无需公告发布权限。",
    ),
    DiscoverySourceDefinition(
        source_id="find_a_tender", name="英国 Find a Tender", category="欧洲采购公告",
        website_url="https://www.find-tender.service.gov.uk/",
        docs_url="https://www.find-tender.service.gov.uk/Developer/Documentation",
        access_methods=("公开 OCDS JSON", "官方公开下载"), coverage="英国",
        description="公开采购与授标记录；使用开放读取接口，不配置公告发布凭证。",
    ),
    DiscoverySourceDefinition(
        source_id="contracts_finder", name="英国 Contracts Finder", category="欧洲采购公告",
        website_url="https://www.contractsfinder.service.gov.uk/",
        docs_url="https://www.gov.uk/government/publications/open-contracting",
        access_methods=("公开 OCDS JSON", "官方公开下载"), coverage="英国",
        description="公开采购公告与合同授标，补充英国采购单位及承包商。",
    ),
    DiscoverySourceDefinition(
        source_id="sam_gov", name="SAM.gov Opportunities", category="美洲采购数据",
        website_url="https://sam.gov/", registration_url="https://sam.gov/",
        docs_url="https://open.gsa.gov/api/get-opportunities-public-api/",
        access_methods=("官方 API",), coverage="美国联邦采购",
        description="采购机会、机构、分类、截止时间和公开联系人；个人账号申请公开读取 key。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="canadabuys", name="CanadaBuys", category="美洲采购数据",
        website_url="https://canadabuys.canada.ca/",
        docs_url="https://open.canada.ca/data/en/dataset/6abd20d4-7a1c-4b38-baa2-9525d0bb2fd2?res_page=1",
        access_methods=("官方 CSV 下载",), coverage="加拿大",
        description="开放招标公告、采购单位和截止时间；下载可用性需接入验证。",
    ),
    DiscoverySourceDefinition(
        source_id="pncp", name="巴西 PNCP", category="美洲采购数据",
        website_url="https://pncp.gov.br/",
        docs_url="https://pncp.gov.br/api/consulta/swagger-ui/index.html",
        access_methods=("公开查询 API",), coverage="巴西",
        description="采购与合同公开查询；查询与写入接口分开，和 Compras 按原始标识去重。",
    ),
    DiscoverySourceDefinition(
        source_id="compras_gov_br", name="巴西 Compras.gov.br", category="美洲采购数据",
        website_url="https://www.gov.br/compras/",
        docs_url="https://www.gov.br/compras/pt-br/cidadao/portal-de-dados-abertos/portal-de-dados-abertos",
        access_methods=("公开 REST API",), coverage="巴西政府采购",
        description="补充采购记录；与 PNCP 的重叠资料不重复计为新增企业。",
    ),
    DiscoverySourceDefinition(
        source_id="world_bank", name="World Bank Procurement Notices", category="国际项目采购",
        website_url="https://financesone.worldbank.org/procurement-notices-from-energy-sector/DS01595",
        docs_url="https://financesone.worldbank.org/procurement-notices-from-energy-sector/DS01595",
        access_methods=("公开采购 API", "官方公开下载"), coverage="多国世行融资项目",
        description="采购事项与当地实施机构；世行项目不等于由世行统一采购。",
    ),
    DiscoverySourceDefinition(
        source_id="ungm", name="UNGM", category="国际项目采购",
        website_url="https://www.ungm.org/Public/Notice",
        docs_url="https://www.ungm.org/Public/Notice",
        access_methods=("公开公告网页", "受控浏览器（待接入）"), coverage="联合国系统、多交付国家",
        description="公告、资格、交付地点与期限；先使用公开页面，供应商注册不等于开发者 API 授权。",
        integration_note="专用公告采集待接入；普通账号 API 权限未确认，无 API key 配置字段。",
    ),
    DiscoverySourceDefinition(
        source_id="importyeti", name="ImportYeti", category="海关与交易数据",
        website_url="https://www.importyeti.com/", registration_url="https://www.importyeti.com/",
        docs_url="https://docs.importyeti.com/docs/getting-started",
        access_methods=("官方 API",), coverage="美国、墨西哥进出口资料，依账号权限",
        description="按企业、货描和交易关系发现企业；大部分 API 需要 credits，不保证提供邮箱。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="volza", name="Volza", category="海关与交易数据",
        website_url="https://www.volza.com/",
        registration_url="https://www.volza.com/trade-intelligence-apis/",
        docs_url="https://www.volza.com/trade-intelligence-apis/",
        access_methods=("商务开通 API",), coverage="多国，按国家和字段授权",
        description="按产品、HS 与贸易主体发现进出口商；费用和权限需商务确认。",
        credential_mode="commercial_pending", integration_note=_COMMERCIAL_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="panjiva", name="Panjiva", category="海关与交易数据",
        website_url="https://panjiva.com/", registration_url="https://panjiva.com/api-guide/",
        docs_url="https://panjiva.com/api-guide/authentication", access_methods=("商务开通 API",),
        coverage="多来源交易记录，按账号授权",
        description="企业交易关系和记录检索；需要正式 API 权限及报价。",
        credential_mode="oauth_client", credential_fields=(
            CredentialField(name="client_id", label="Client ID"),
            CredentialField(name="client_secret", label="Client secret"),
        ),
        integration_note="采集待接入；商务 API 权限仍需开通。客户端凭证换取的访问令牌有效期为 24 小时。",
    ),
    DiscoverySourceDefinition(
        source_id="importgenius", name="ImportGenius", category="海关与交易数据",
        website_url="https://www.importgenius.com/",
        registration_url="https://w3.importgenius.com/features/api-integration",
        docs_url="https://w3.importgenius.com/features/api-integration",
        access_methods=("商务开通 REST API",), coverage="按订阅国家和授权字段",
        description="指定国家的贸易记录；网页套餐不自动包含 API，交易记录不保证带邮箱。",
        credential_mode="access_token",
        credential_fields=(CredentialField(name="access_token", label="Access token"),),
        integration_note="采集待接入；可先配置 access_token，国家、字段及 API 权限仍需商务开通。",
    ),
    DiscoverySourceDefinition(
        source_id="company_websites", name="企业官网、联系页与公开店铺", category="原有公开来源",
        website_url=None, access_methods=_PAGE_METHODS, coverage="按目标企业确定网址",
        description="企业经营范围、产品品牌、公开业务邮箱、门店和分支。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="industry_directories", name="行业企业名录", category="行业目录与渠道",
        website_url=None, access_methods=_PAGE_METHODS, coverage="按国家与行业选择实际目录",
        description="通过目录定位相关企业和官网，区分买方、卖方和服务商。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="trade_associations", name="行业协会会员目录", category="行业目录与渠道",
        website_url=None, access_methods=("官方导出 / API（逐站）", *_PAGE_METHODS),
        coverage="按行业和协会选择", description="发现垂直行业企业；目录站点不等于企业官网。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="chambers", name="商会会员网络", category="行业目录与渠道",
        website_url=None, access_methods=("官方导出 / API（逐站）", *_PAGE_METHODS),
        coverage="按国家与当地商会选择", description="当地中小企业和贸易网络；逐站确认目录复用方式。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="trade_fairs", name="展会展商目录", category="行业目录与渠道",
        website_url=None, access_methods=("官方导出 / API（逐站）", *_PAGE_METHODS),
        coverage="按行业、展会及届次选择", description="品牌商、配套商和分销商；参展商不等于采购商。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="dealer_networks", name="品牌授权经销商网络", category="行业目录与渠道",
        website_url=None, access_methods=("官方导出 / API（逐站）", *_PAGE_METHODS),
        coverage="按品牌与授权市场选择", description="经营目标品类的经销商、渠道商及其官网。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="company_procurement", name="企业采购、供应商招募与 RFQ", category="原有公开来源",
        website_url=None, access_methods=("公开 API（逐站）", "RSS / 订阅", *_PAGE_METHODS),
        coverage="按企业采购门户确定网址", description="具体采购类别、资格、供应商准入和时间窗口。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="public_procurement_pages", name="公开采购公告网页检索", category="原有公开来源",
        website_url=None, access_methods=_PAGE_METHODS, coverage="采购门户和公告原站",
        description="保留既有公告搜索；与新增采购 API、下载记录按原始来源去重。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="company_news", name="企业新闻、公告与公开媒体", category="原有公开来源",
        website_url=None, access_methods=_PAGE_METHODS, coverage="按企业与市场选择",
        description="扩建、新工厂、门店和项目中标等需求信号，不直接代表已确认需求。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="rss", name="企业与公告 RSS 订阅", category="原有公开来源",
        website_url=None, access_methods=("RSS / Atom 订阅",), coverage="具有公开订阅源的企业或公告站",
        description="按更新增量读取，减少反复扫描未变化页面。",
        integration_note="RSS 持续采集待接入；公开新闻页面已有通用读取路径。",
    ),
    DiscoverySourceDefinition(
        source_id="linkedin_public", name="公开 LinkedIn 公司页面", category="原有公开来源",
        website_url="https://www.linkedin.com/", access_methods=("公开搜索索引", "允许访问的公司页"),
        coverage="公开可访问的公司资料",
        description="原有公开公司检索方向；未接入 LinkedIn 联系人 API，不绕过登录或访问限制。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="trade_record_indexes", name="公开贸易记录索引", category="原有公开来源",
        website_url=None, access_methods=_PAGE_METHODS, coverage="按检索结果与公开权限确定",
        description="保留既有贸易记录页面检索；商业交易 API 单独配置与授权。",
        integration_status="partial", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="codex_cli", name="Codex CLI", category="模型执行支持",
        website_url="https://learn.chatgpt.com/docs/web-search",
        docs_url="https://learn.chatgpt.com/docs/web-search", access_methods=("工具规划", "公开事实提取"),
        coverage="本库存网页采集任务", description="规划网关工具调用并提取公开事实；内置网页搜索未启用。",
        credential_mode="managed_session",
        integration_status="partial",
        integration_note="本库存运行器已装配，内置搜索未启用；模型连接与账号运行另行核实，不在此填写模型密钥。",
    ),
    DiscoverySourceDefinition(
        source_id="brave", name="Brave Search", category="搜索入口",
        website_url="https://search.brave.com/",
        registration_url="https://api-dashboard.search.brave.com/",
        docs_url="https://api-dashboard.search.brave.com/documentation/pricing",
        access_methods=("搜索 API",), coverage="公开网页搜索",
        description="定位企业及资料原站；搜索结果不等于新增客户。",
        credential_mode="api_key", credential_fields=_API_KEY, integration_status="available",
        integration_note="搜索连接器已有实现；保存新密钥不自动启用生产，仍需运行配置及就绪验证。",
    ),
    DiscoverySourceDefinition(
        source_id="tavily", name="Tavily", category="搜索入口",
        website_url="https://www.tavily.com/", docs_url="https://www.tavily.com/terms",
        access_methods=("搜索 API",), coverage="公开网页搜索",
        description="已有研究搜索实现；条款限制 unsolicited marketing proposals 用途。",
        credential_mode="api_key", credential_fields=_API_KEY, integration_status="available",
        integration_note="已为本库存网页发现提供搜索支持；运行配置、账户额度和用途仍需核实，不作为独立企业来源计数。",
    ),
    DiscoverySourceDefinition(
        source_id="zerobounce", name="ZeroBounce", selection="backup", category="邮箱验证备选",
        website_url="https://www.zerobounce.net/services",
        registration_url="https://www.zerobounce.net/members/signin/register",
        access_methods=("官方 API",), coverage="邮箱可达性验证",
        description="邮箱验证与可选查找能力，不作为企业发现数据库。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="singapore_data", name="新加坡 data.gov.sg", selection="backup", category="采购数据备选",
        website_url="https://data.gov.sg/datasets/d_acde1106003906a75c3fa052592f2fcb/view",
        access_methods=("历史数据下载", "开放数据 API"), coverage="新加坡历史授标记录",
        description="发现历史中标企业；历史授标数据不是实时采购公告。",
    ),
    DiscoverySourceDefinition(
        source_id="gebiz", name="新加坡 GeBIZ", selection="backup", category="采购数据备选",
        website_url="https://www.gebiz.gov.sg/", access_methods=_PAGE_METHODS, coverage="新加坡",
        description="当前采购事项公开网页，与历史授标数据区分。",
    ),
    DiscoverySourceDefinition(
        source_id="gdelt", name="GDELT", selection="backup", category="事件与网页数据备选",
        website_url="https://gdeltproject.org/data.html", docs_url="https://gdeltproject.org/data.html",
        access_methods=("新闻 API", "开放数据下载"), coverage="多国新闻事件",
        description="扩建、新工厂等事件线索；须回到原文确认企业和事件。",
    ),
    DiscoverySourceDefinition(
        source_id="greenhouse", name="Greenhouse Job Board API", selection="backup",
        category="事件与网页数据备选", website_url="https://docs.greenhouse.io/job-board.html",
        docs_url="https://docs.greenhouse.io/job-board.html", access_methods=("公开 GET API",),
        coverage="已知企业的招聘 board", description="企业招聘动态，不是全球企业搜索接口。",
    ),
    DiscoverySourceDefinition(
        source_id="openstreetmap", name="OpenStreetMap / Overpass", selection="backup",
        category="事件与网页数据备选", website_url="https://www.openstreetmap.org/",
        docs_url="https://dev.overpass-api.de/overpass-doc/en/preface/commons.html",
        access_methods=("Overpass 开放查询",), coverage="全球地图，完整度因地区而异",
        description="当地门店、批发网点和可用的网站字段；资料稀疏，不保证邮箱。",
    ),
    DiscoverySourceDefinition(
        source_id="common_crawl", name="Common Crawl", selection="backup", category="事件与网页数据备选",
        website_url="https://index.commoncrawl.org/", docs_url="https://index.commoncrawl.org/",
        access_methods=("开放索引查询", "批量数据下载"), coverage="公开网页历史抓取",
        description="离线域名和网站发现；需处理过期资料及存储计算成本。",
    ),
    DiscoverySourceDefinition(
        source_id="gleif", name="GLEIF", selection="backup", category="企业身份资料备选",
        website_url="https://www.gleif.org/en/lei-data/gleif-api/",
        docs_url="https://www.gleif.org/en/lei-data/gleif-api/", access_methods=("公开 API",),
        coverage="全球持有 LEI 的法律实体", description="法律主体与关系核实，不是完整企业库或邮箱库。",
    ),
    DiscoverySourceDefinition(
        source_id="companies_house", name="英国 Companies House", selection="backup",
        category="企业身份资料备选", website_url="https://developer.company-information.service.gov.uk/",
        registration_url="https://developer.company-information.service.gov.uk/",
        docs_url="https://developer.company-information.service.gov.uk/", access_methods=("官方 API",),
        coverage="英国企业登记", description="注册主体消歧和资料核实，不代表采购意向。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="insee", name="法国 INSEE Sirene", selection="backup", category="企业身份资料备选",
        website_url="https://www.data.gouv.fr/dataservices/api-sirene-open-data",
        docs_url="https://www.data.gouv.fr/dataservices/api-sirene-open-data",
        access_methods=("官方开放数据 API",), coverage="法国企业与营业场所",
        description="企业主体核实和消歧，不是邮箱或采购意向库。",
        credential_mode="authentication_pending",
        integration_note="采集待接入；具体端点的认证要求待核实后添加，不预设凭证字段。",
    ),
    DiscoverySourceDefinition(
        source_id="opencorporates", name="OpenCorporates", selection="backup", category="企业身份资料备选",
        website_url="https://opencorporates.com/",
        docs_url="https://api.opencorporates.com/documentation/API-Reference",
        access_methods=("官方 API",), coverage="多国企业注册资料",
        description="主体消歧；商业闭源使用需核实许可，不能套用开放项目免费授权。",
        credential_mode="api_key", credential_fields=(CredentialField(name="api_key", label="API token"),),
    ),
    DiscoverySourceDefinition(
        source_id="serper", name="Serper", selection="backup", category="搜索入口",
        website_url="https://serper.dev/", registration_url="https://serper.dev/",
        docs_url="https://serper.dev/", access_methods=("搜索 API",), coverage="公开网页搜索",
        description="按搜索覆盖缺口选用；无需为开通其他来源先购买。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="exa", name="Exa", selection="backup", category="搜索入口",
        website_url="https://exa.ai/", docs_url="https://exa.ai/docs/admin/pricing",
        access_methods=("搜索 API",), coverage="公开网页搜索",
        description="搜索补充备选，按有效数据增量与成本评估。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="serpapi", name="SerpApi", selection="backup", category="搜索入口",
        website_url="https://serpapi.com/", docs_url="https://serpapi.com/pricing",
        access_methods=("搜索 API",), coverage="搜索引擎结果",
        description="搜索补充备选，同企业跨引擎重复结果需去重。",
        credential_mode="api_key", credential_fields=_API_KEY,
    ),
    DiscoverySourceDefinition(
        source_id="blum", name="Blum 经销商目录", selection="example", category="行业目录示例",
        website_url="https://www.blum.com/us/en/contact/wheretobuy/distributors/",
        access_methods=("公开目录页面",), coverage="示例品牌与市场",
        description="品牌授权经销商方向示例，按实际产品和地区选择。", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="nfda", name="NFDA 会员目录", selection="example", category="行业目录示例",
        website_url="https://www.nfda-fastener.org/member-list1",
        access_methods=("公开会员目录",), coverage="紧固件行业示例",
        description="垂直行业会员企业，需区分买方、卖方和服务商。", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="efda", name="EFDA", selection="example", category="行业目录示例",
        website_url="https://www.efda-fastenerdistributors.com/",
        access_methods=("公开协会页面",), coverage="欧洲紧固件分销行业示例",
        description="行业协会和渠道网络入口，逐站确定可读目录。", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="sicci", name="SICCI 商会会员目录", selection="example", category="行业目录示例",
        website_url="https://sicci.com/member-directory/", access_methods=("公开会员目录",),
        coverage="新加坡商会示例", description="当地企业与贸易网络，按目标行业筛选。",
        integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="smcci", name="SMCCI 商会会员目录", selection="example", category="行业目录示例",
        website_url="https://smcci.org.sg/membership-directory/", access_methods=("公开会员目录",),
        coverage="新加坡商会示例", description="当地中小企业网络，批量读取方式逐站确认。",
        integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="eisenwarenmesse", name="科隆五金展展商目录", selection="example", category="行业目录示例",
        website_url="https://www.eisenwarenmesse.com/eisenwarenmesse-exhibitors/list-of-exhibitors/",
        access_methods=("官方展商检索", "允许的公开导出"), coverage="五金展会示例，按届次选择",
        description="品牌商、配套商和分销商；参展身份不等于采购需求。", integration_note=_PAGE_NOTE,
    ),
    DiscoverySourceDefinition(
        source_id="ish", name="ISH 展商目录", selection="example", category="行业目录示例",
        website_url="https://ish.messefrankfurt.com/frankfurt/en/exhibitor-search.html",
        access_methods=("官方展商检索", "允许的公开导出"), coverage="建筑及卫浴相关展会示例",
        description="按产品匹配寻找展商和渠道，需区分企业角色。", integration_note=_PAGE_NOTE,
    ),
)


def get_discovery_source(source_id: str) -> DiscoverySourceDefinition | None:
    """只返回登记过的来源，未知标识不能变成任意凭证存储入口。"""
    return next((item for item in DISCOVERY_SOURCES if item.source_id == source_id), None)
