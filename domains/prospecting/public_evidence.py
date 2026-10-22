"""受信采集器事实投影；自由模型输出不能提供URL、哈希、时间与原件。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from shared.schemas.identifiers import PublicCandidateId, new_id
from shared.schemas.provenance import Provenance, SourceType

from .inventory import PublicCandidate
from .projection_diagnostic import RejectionCounts


class ProjectionRejected(ValueError):
    """证据门的固定首个拒绝理由，不读取或携带输入与异常原文。"""

    def __init__(self, reason: str) -> None:
        if reason not in RejectionCounts.model_fields:
            raise ValueError("投影拒绝类型无效")
        super().__init__("公开证据未通过校验")
        self.reason = reason


COUNTRY_NAMES = {
    "KE": "Kenya",
    "TZ": "Tanzania",
    "UG": "Uganda",
    "NG": "Nigeria",
    "ZA": "South Africa",
    "GH": "Ghana",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
    "RW": "Rwanda",
    "ET": "Ethiopia",
    "EG": "Egypt",
    "MA": "Morocco",
    "SN": "Senegal",
    "CM": "Cameroon",
    "CI": "Côte d’Ivoire",
    "AO": "Angola",
    "MZ": "Mozambique",
    "MW": "Malawi",
    "BW": "Botswana",
    "NA": "Namibia",
    "US": "United States",
    "GB": "United Kingdom",
    "SG": "Singapore",
    "IN": "India",
    "CN": "China",
    "DE": "Germany",
    "FR": "France",
}
COUNTRY_NAMES.update(
    {
        "DZ": "Algeria",
        "LY": "Libya",
        "SD": "Sudan",
        "TN": "Tunisia",
        "EH": "Western Sahara",
        "BI": "Burundi",
        "KM": "Comoros",
        "DJ": "Djibouti",
        "ER": "Eritrea",
        "MG": "Madagascar",
        "MU": "Mauritius",
        "YT": "Mayotte",
        "RE": "Réunion",
        "SC": "Seychelles",
        "SO": "Somalia",
        "SS": "South Sudan",
        "CF": "Central African Republic",
        "TD": "Chad",
        "CG": "Republic of the Congo",
        "CD": "Democratic Republic of the Congo",
        "GQ": "Equatorial Guinea",
        "GA": "Gabon",
        "ST": "São Tomé and Príncipe",
        "SZ": "Eswatini",
        "LS": "Lesotho",
        "BJ": "Benin",
        "BF": "Burkina Faso",
        "CV": "Cabo Verde",
        "GM": "Gambia",
        "GN": "Guinea",
        "GW": "Guinea-Bissau",
        "LR": "Liberia",
        "ML": "Mali",
        "MR": "Mauritania",
        "NE": "Niger",
        "SH": "Saint Helena",
        "SL": "Sierra Leone",
        "TG": "Togo",
    }
)

TAG_ALIASES = {
    "三轮车": ("tricycle", "three wheeler", "three-wheeler", "tuk tuk"),
    "电动三轮车": ("electric tricycle", "electric three-wheeler"),
}


# 标签必须表达当前页主体所在地，不能只在任意叙述中出现 address 一词。
_LOCATION_LABEL = re.compile(
    r"^(?:our\s+)?(?:(?:registered|head)\s+)?"
    r"(?:address|office|location|headquarters)(?:\s+address)?\s*[:：-]\s*(.*)$",
    re.IGNORECASE,
)
_COUNTEREVIDENCE = re.compile(
    r"\?|\b(?:may|maybe|might|could|possible|possibly|potential|planned|proposed|planning|future|"
    r"not|never|no|formerly|previously|export\w*|ship(?:s|ping|ped)?|"
    r"deliver\w*|serve\w*|sell|sales\s+to|markets|marketing|target\s+market|coverage|"
    r"(?:customer|client|supplier|partner|distributor|dealer)(?:s|[’']s)?\s+"
    r"(?:address|office|depot|location|headquarters)|"
    r"(?:our|the|a)\s+(?:customer|client|supplier|partner|distributor|dealer)\b|"
    r"news|reported|according\s+to)\b",
    re.IGNORECASE,
)
_STREET = re.compile(
    r"^\d{1,6}(?:[-/]\d{1,6})?\s+[^\W\d_][\w .’'-]{0,90}\s+"
    r"(?:Road|Rd|Street|St|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd)\.?"
    r"(?P<localities>(?:,\s*[^,]+)+)$",
    re.IGNORECASE,
)


def _country_address_proven(text: str, country: str, country_name: str) -> bool:
    """只认有界所在地块；词表与相邻块不能替代完整的地址归属理解。"""
    names = {value.casefold(): code for code, value in COUNTRY_NAMES.items()}
    pattern = re.compile(
        r"(?<!\w)(?:"
        + "|".join(re.escape(value) for value in sorted(names, key=len, reverse=True))
        + r")(?!\w)",
        re.IGNORECASE,
    )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if len(line) > 240:
            continue
        label = _LOCATION_LABEL.fullmatch(line)
        previous = _LOCATION_LABEL.fullmatch(lines[index - 1]) if index else None
        labelled = bool(label and label[1]) or bool(previous and not previous[1])
        street = _STREET.fullmatch(line)
        full_street = False
        if street:
            segments = [part.strip() for part in street["localities"].split(",")[1:]]
            full_street = (
                len(segments) >= 2
                and segments[-1].removesuffix(".").casefold() == country_name.casefold()
                and all(
                    re.fullmatch(r"[\w .’'-]+", part)
                    and any(char.isalpha() for char in part)
                    for part in segments[:-1]
                )
            )
        if not (labelled or full_street):
            continue
        if {names[m.group().casefold()] for m in pattern.finditer(line)} != {country}:
            continue
        context = "\n".join(lines[max(0, index - 2) : index + 3])
        if len(context) > 1000 or _COUNTEREVIDENCE.search(context):
            continue
        if {names[m.group().casefold()] for m in pattern.finditer(context)} != {
            country
        }:
            continue
        return True
    return False


def project_candidate(
    *,
    source_id: str,
    source_url: str,
    text: str,
    content_hash: str,
    artifact_id: str,
    observed_at: datetime,
    name: str,
    country: str,
    product_tags: tuple[str, ...],
    public_emails: tuple[str, ...],
) -> PublicCandidate:
    """国家仅接受明确所在地标签或完整街道地址；产品标签是内容匹配而不是采购需求。"""
    if re.search(
        r"ignore (?:all |previous |prior )*(?:instructions|prompts)|system prompt|developer message|忽略.{0,12}(?:指令|提示)",
        text,
        re.IGNORECASE,
    ):
        raise ProjectionRejected("instruction_content")
    lowered = text.casefold()
    country_name = COUNTRY_NAMES.get(country)
    if not name.strip() or name.casefold() not in lowered:
        raise ProjectionRejected("company_name_unproven")
    if country_name is None:
        raise ProjectionRejected("country_unsupported")
    if not _country_address_proven(text, country, country_name):
        raise ProjectionRejected("country_address_unproven")
    for tag in product_tags:
        if not any(
            alias.casefold() in lowered for alias in TAG_ALIASES.get(tag, (tag,))
        ):
            raise ProjectionRejected("product_unproven")
    emails = {
        e.casefold()
        for e in re.findall(
            r"[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text
        )
    }
    if any(email.casefold() not in emails for email in public_emails):
        raise ProjectionRejected("email_unproven")
    try:
        domain = urlsplit(source_url).hostname
    except ValueError:
        raise ProjectionRejected("source_url_invalid") from None
    if not domain:
        raise ProjectionRejected("source_url_invalid")
    # 保守官网身份门：目录里的企业名和别家邮箱不能把目录host升格成企业。
    normalized_host = re.sub(
        r"[^a-z0-9]", "", domain.casefold().removeprefix("www.").split(".")[0]
    )
    words = set(re.findall(r"[a-z0-9]{3,}", name.casefold())) - {
        "company",
        "limited",
        "ltd",
        "inc",
        "corp",
        "corporation",
        "group",
        "international",
        "the",
    }
    if not any(word in normalized_host for word in words):
        raise ProjectionRejected("company_domain_unproven")
    if any(
        email.casefold().rsplit("@", 1)[-1] != domain.casefold().removeprefix("www.")
        for email in public_emails
    ):
        raise ProjectionRejected("email_domain_mismatch")
    try:
        return PublicCandidate(
            id=PublicCandidateId(new_id("pci")),
            canonical_domain=domain,
            name=name,
            country=country,
            product_tags=product_tags,
            public_emails=public_emails,
            source_id=source_id,
            source_url=source_url,
            evidence_text=text[:20000],
            observed_at=observed_at,
            expires_at=observed_at + timedelta(days=30),
            provenance=Provenance(
                SourceType.WEB_PAGE,
                artifact_id,
                "public-evidence-v2",
                observed_at,
                source_url=source_url,
                page_hash=content_hash,
            ),
        )
    except ValueError:
        raise ProjectionRejected("candidate_contract_invalid") from None
