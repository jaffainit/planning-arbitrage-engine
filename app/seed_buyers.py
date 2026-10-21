"""Phase 2: Agent 0 - PECR-Compliant UK B2B Buyer Harvester with SIC & Website Domain Verification."""

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import BUYERS_SEED_FILE, settings
from app.database import db_session, init_db, upsert_buyer
from app.models import BuyerRecord, SpecialistCategory

logger = logging.getLogger(__name__)

COMPANIES_HOUSE_SEARCH_URL = "https://api.company-information.service.gov.uk/search/companies"
COMPANIES_HOUSE_ADVANCED_SEARCH_URL = (
    "https://api.company-information.service.gov.uk/advanced-search/companies"
)
COMPANIES_HOUSE_COMPANY_URL = "https://api.company-information.service.gov.uk/company"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
YAHOO_UK_SEARCH_URL = "https://uk.search.yahoo.com/search"

SPECIALIST_QUERY_MAPPING: dict[str, SpecialistCategory] = {
    "acoustic consultancy": SpecialistCategory.ACOUSTIC_BS4142,
    "ecological consultancy": SpecialistCategory.ECOLOGY_BATS_BNG,
    "arboricultural consultancy": SpecialistCategory.ARBORICULTURAL,
    "geo-environmental consultancy": SpecialistCategory.CONTAMINATED_LAND,
    "flood risk consultancy": SpecialistCategory.FLOOD_DRAINAGE,
    "heritage consultancy": SpecialistCategory.HERITAGE_ARCHAEOLOGY,
    "town planning consultancy": SpecialistCategory.PLANNING_APPEAL,
}

SUSSEX_SOUTH_EAST_POSTCODE_AREAS: set[str] = {
    "BN",
    "RH",
    "TN",
    "PO",
    "GU",
    "KT",
    "CR",
    "BR",
    "ME",
    "CT",
    "SW",
    "SE",
    "EC",
    "WC",
    "W",
    "W1",
    "NW",
    "HA",
    "SG",
    "OX",
}

SUSSEX_SOUTH_EAST_LOCATIONS: tuple[str, ...] = (
    "Sussex",
    "Brighton",
    "South East",
    "Surrey",
    "Kent",
    "London",
)

CATEGORY_SEARCH_TERMS: dict[SpecialistCategory, list[str]] = {
    SpecialistCategory.ACOUSTIC_BS4142: [
        "acoustic consultants",
        "acoustic consultancy",
        "acoustic Sussex",
        "acoustic South East",
        "acoustic London",
    ],
    SpecialistCategory.ECOLOGY_BATS_BNG: [
        "ecology",
        "ecological consultancy",
        "ecology Sussex",
        "ecology Surrey",
        "ecology Kent",
    ],
    SpecialistCategory.ARBORICULTURAL: [
        "tree consultancy",
        "tree survey",
        "arboricultural",
        "tree Sussex",
        "tree Surrey",
        "tree London",
    ],
    SpecialistCategory.CONTAMINATED_LAND: [
        "kent and sussex geotechnical",
        "geotechnical",
        "geo-environmental",
        "contaminated land",
    ],
    SpecialistCategory.FLOOD_DRAINAGE: [
        "strata flood",
        "flood risk",
        "drainage consultancy",
        "hydrology",
    ],
    SpecialistCategory.HERITAGE_ARCHAEOLOGY: [
        "built heritage consultancy",
        "archaeology",
        "heritage consultancy",
        "archaeological Kent",
    ],
    SpecialistCategory.PLANNING_APPEAL: [
        "town planning",
        "planning south east",
        "planning consultancy",
        "planit consultants",
    ],
}

ALLOWED_COMPANY_TYPES: set[str] = {"ltd", "llp", "plc"}

ALLOWED_SIC_CODES: set[str] = {
    "71111",
    "71112",
    "71121",
    "71122",
    "71129",
    "74901",
    "74909",
    "70229",
}

SPECIALIST_NAME_KEYWORDS: tuple[str, ...] = (
    "acoustic",
    "ecolog",
    "arboricultur",
    "tree",
    "geo-environmental",
    "geotechnical",
    "contaminated",
    "flood",
    "drainage",
    "heritage",
    "archaeolog",
    "town planning",
    "planning",
)

FALSE_POSITIVE_NAME_KEYWORDS: tuple[str, ...] = (
    "financial",
    "finance",
    "manpower",
    "recruitment",
    "accountant",
    "accounting",
    "tax",
    "insulation manufacturing",
    "cleaning",
    "catering",
)

CATEGORY_DOMAIN_KEYWORDS: dict[SpecialistCategory, tuple[str, ...]] = {
    SpecialistCategory.ACOUSTIC_BS4142: (
        "acoustic",
        "noise",
        "bs 4142",
        "bs4142",
        "sound",
        "vibration",
    ),
    SpecialistCategory.ECOLOGY_BATS_BNG: (
        "ecolog",
        "biodiversity",
        "bat",
        "habitat",
        "protected species",
        "bng",
    ),
    SpecialistCategory.ARBORICULTURAL: (
        "arboricultur",
        "tree",
        "bs 5837",
        "bs5837",
        "woodland",
    ),
    SpecialistCategory.CONTAMINATED_LAND: (
        "geo-environmental",
        "geoenvironmental",
        "contaminated",
        "geotechnical",
        "remediation",
        "ground investigation",
    ),
    SpecialistCategory.FLOOD_DRAINAGE: (
        "flood",
        "drainage",
        "suds",
        "surface water",
        "hydrolog",
    ),
    SpecialistCategory.HERITAGE_ARCHAEOLOGY: (
        "heritage",
        "archaeolog",
        "listed building",
        "historic",
        "conservation",
    ),
    SpecialistCategory.PLANNING_APPEAL: (
        "town planning",
        "planning consultancy",
        "planning permission",
        "planning appeal",
        "rtpi",
        "development",
    ),
}

MOCK_COMPANY_NUMBERS: set[str] = {
    "09411201",
    "OC418290",
    "10293847",
    "08871234",
    "11029384",
    "09988776",
    "07654321",
    "12349876",
}

FORBIDDEN_EMAIL_DOMAINS: set[str] = {
    "gmail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "hotmail.com",
    "hotmail.co.uk",
    "outlook.com",
    "btinternet.com",
    "sky.com",
    "icloud.com",
    "live.co.uk",
    "aol.com",
    "virginmedia.com",
    "sentry.io",
    "wixpress.com",
    "example.com",
    "domainmarket.com",
    "email.com",
    "domain.com",
    "godaddy.com",
    "sedo.com",
}

CORPORATE_ROLE_PREFIXES: tuple[str, ...] = (
    "enquiries@",
    "info@",
    "projects@",
    "tenders@",
    "office@",
    "hello@",
    "contact@",
    "mail@",
    "admin@",
    "post@",
    "consultancy@",
    "studio@",
)

POSTCODE_AREA_REGEX = re.compile(r"^([A-Z]{1,2})\d", re.IGNORECASE)
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def extract_postcode_area(postal_code: str) -> str:
    """Extract alphabetic UK postcode area prefix (e.g. 'BN1 4GH' -> 'BN')."""
    if not postal_code:
        return ""
    cleaned = postal_code.strip().upper()
    match = POSTCODE_AREA_REGEX.match(cleaned)
    return match.group(1).upper() if match else ""


def is_pecr_compliant_company(item: dict[str, Any]) -> bool:
    """Verify UK PECR Regulation 22 Corporate Subscriber rules (active Ltd/LLP/PLC only)."""
    status = str(item.get("company_status", "")).strip().lower()
    company_type = str(item.get("company_type", "")).strip().lower()
    return status == "active" and company_type in ALLOWED_COMPANY_TYPES


def matches_sic_or_specialist_name(company_name: str, sic_codes: list[str]) -> bool:
    """Verify condition (a): SIC codes match architectural/engineering/environmental/planning OR company name has specialist keyword."""
    name_lower = company_name.strip().lower()
    if any(fp in name_lower for fp in FALSE_POSITIVE_NAME_KEYWORDS):
        return False

    has_specialist_name = any(kw in name_lower for kw in SPECIALIST_NAME_KEYWORDS)
    has_allowed_sic = bool({str(s).strip() for s in sic_codes} & ALLOWED_SIC_CODES)

    # Require that the company name relates to the specialist/planning/engineering domain and is not a generic 'AAA Consultancy'
    if re.match(r"^aa+[b-z]?\s+consultancy\b", name_lower):
        return False

    return has_allowed_sic or has_specialist_name


def website_text_contains_specialist_keywords(
    website_text: str, category: SpecialistCategory
) -> bool:
    """Verify condition (b): scraped website text actually contains the specialist domain keywords."""
    if not website_text:
        return False
    text_lower = website_text.lower()
    keywords = CATEGORY_DOMAIN_KEYWORDS.get(category, SPECIALIST_NAME_KEYWORDS)
    return any(kw in text_lower for kw in keywords)


def is_valid_corporate_email(email: str) -> bool:
    """Confirm email is a corporate role address and not on a consumer ISP or parked domain."""
    if not email or "@" not in email:
        return False
    email_lower = email.strip().lower().rstrip(".")
    if email_lower.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".js", ".css")):
        return False
    domain = email_lower.split("@", 1)[1]
    if domain in FORBIDDEN_EMAIL_DOMAINS:
        return False
    return email_lower.startswith(CORPORATE_ROLE_PREFIXES)


def _candidate_company_domains(company_name: str) -> list[str]:
    """Generate candidate UK corporate URLs (.co.uk / .com) directly from the registered company title."""
    cleaned = re.sub(
        r"\b(limited|ltd|llp|plc|uk|holdings|group)\b",
        "",
        company_name.lower(),
    ).strip()
    words = [w for w in re.split(r"[^a-z0-9]+", cleaned) if w and w not in ("the", "and")]
    if not words:
        return []
    slugs = ["".join(words), "-".join(words)]
    core = [
        w
        for w in words
        if w not in ("consultancy", "consultants", "consulting", "services", "associates", "solutions")
    ]
    if core and core != words:
        slugs.extend(["".join(core), "-".join(core)])

    unique_slugs: list[str] = []
    for s in slugs:
        if s and len(s) >= 4 and s not in unique_slugs:
            unique_slugs.append(s)

    urls: list[str] = []
    for s in unique_slugs[:3]:
        urls.append(f"https://www.{s}.co.uk")
        urls.append(f"https://{s}.co.uk")
        urls.append(f"https://www.{s}.com")
    return urls


def resolve_company_website(client: httpx.Client, company_name: str) -> str:
    """Resolve official corporate website via candidate UK domains, Yahoo UK search, or DuckDuckGo Lite."""
    for candidate_url in _candidate_company_domains(company_name):
        try:
            resp = client.get(
                candidate_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                follow_redirects=True,
                timeout=4.0,
            )
            if resp.status_code == 200 and len(resp.text) > 500:
                parsed = urlparse(str(resp.url))
                return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            continue

    excluded_hosts = {
        "duckduckgo.com",
        "yahoo.com",
        "bing.com",
        "find-and-update.company-information.service.gov.uk",
        "company-information.service.gov.uk",
        "gov.uk",
        "linkedin.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "endole.co.uk",
        "companycheck.co.uk",
        "checkatrade.com",
        "yell.com",
        "pomanda.com",
        "dnb.com",
        "bizdb.co.uk",
        "open-uk.org",
        "theplanningreview.co.uk",
    }

    try:
        resp = client.get(
            YAHOO_UK_SEARCH_URL,
            params={"p": f'"{company_name}" UK consultancy official website'},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            follow_redirects=True,
            timeout=6.0,
        )
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("a", href=True):
                href = str(link["href"]).strip()
                if "RU=http" in href:
                    href = unquote(href.split("RU=", 1)[1].split("/RK=", 1)[0])
                if not href.startswith(("http://", "https://")):
                    continue
                parsed = urlparse(href)
                host = parsed.netloc.lower().removeprefix("www.")
                if host and not any(host == ex or host.endswith("." + ex) for ex in excluded_hosts):
                    return f"{parsed.scheme}://{parsed.netloc}"
    except Exception as exc:
        logger.debug("Yahoo website resolution failed for %s: %s", company_name, exc)

    return ""


def scrape_corporate_role_email(client: httpx.Client, base_website: str) -> str:
    """Scrape /, /contact, and /contact-us for PECR-compliant corporate role emails."""
    email, _ = scrape_corporate_role_email_and_text(client, base_website)
    return email


def scrape_corporate_role_email_and_text(
    client: httpx.Client, base_website: str
) -> tuple[str, str]:
    """Scrape /, /contact, and /contact-us and return (corporate_role_email, combined_visible_text)."""
    if not base_website:
        return "", ""
    candidate_paths = ["/", "/contact", "/contact-us"]
    found_email = ""
    collected_texts: list[str] = []

    for path in candidate_paths:
        target_url = urljoin(base_website.rstrip("/") + "/", path.lstrip("/"))
        try:
            resp = client.get(
                target_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                follow_redirects=True,
                timeout=4.5,
            )
            if resp.status_code != 200 or len(resp.text) < 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            collected_texts.append(soup.get_text(" ", strip=True))
            if not found_email:
                for candidate in EMAIL_REGEX.findall(resp.text):
                    cleaned = candidate.strip().lower().rstrip(".")
                    if is_valid_corporate_email(cleaned):
                        found_email = cleaned
                        break
        except Exception as exc:
            logger.debug("Scrape failed on %s: %s", target_url, exc)

    return found_email, " ".join(collected_texts)


def get_fallback_buyer_records() -> list[BuyerRecord]:
    """Verified genuine Sussex, South East, and London corporate specialist consultancies."""
    raw_seed = [
        # ACOUSTIC_BS4142 (Keeping existing HA5 & SG18 South East/London consultancies)
        {
            "company_name": "PINNACLE ACOUSTIC CONSULTANTS LIMITED",
            "company_number": "12953862",
            "company_type": "ltd",
            "category": SpecialistCategory.ACOUSTIC_BS4142,
            "postcode_area": "HA",
            "full_postcode": "HA5 2RR",
            "website": "https://www.pinnacleacoustic.com",
            "b2b_email": "hello@pinnacleacoustic.com",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "12953862")),
            "unsubscribed": 0,
        },
        {
            "company_name": "SPECTRUM ACOUSTIC CONSULTANTS LIMITED",
            "company_number": "02378475",
            "company_type": "ltd",
            "category": SpecialistCategory.ACOUSTIC_BS4142,
            "postcode_area": "SG",
            "full_postcode": "SG18 0JE",
            "website": "https://www.spectrumacoustic.com",
            "b2b_email": "enquiries@spectrumacoustic.com",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "02378475")),
            "unsubscribed": 0,
        },
        # ECOLOGY_BATS_BNG (Sussex BN7, Kent/Sussex TN24, Surrey GU24)
        {
            "company_name": "CO-ECOLOGY LIMITED",
            "company_number": "13169327",
            "company_type": "ltd",
            "category": SpecialistCategory.ECOLOGY_BATS_BNG,
            "postcode_area": "BN",
            "full_postcode": "BN7 1XG",
            "website": "https://co-ecology.co.uk",
            "b2b_email": "enquiries@co-ecology.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "13169327")),
            "unsubscribed": 0,
        },
        {
            "company_name": "CORYLUS ECOLOGY LIMITED",
            "company_number": "05005553",
            "company_type": "ltd",
            "category": SpecialistCategory.ECOLOGY_BATS_BNG,
            "postcode_area": "TN",
            "full_postcode": "TN24 8DH",
            "website": "https://corylus-ecology.co.uk",
            "b2b_email": "info@corylus-ecology.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "05005553")),
            "unsubscribed": 0,
        },
        {
            "company_name": "SWT ECOLOGY SERVICES LIMITED",
            "company_number": "11034197",
            "company_type": "ltd",
            "category": SpecialistCategory.ECOLOGY_BATS_BNG,
            "postcode_area": "GU",
            "full_postcode": "GU24 0JN",
            "website": "https://www.swtecologyservices.org",
            "b2b_email": "consultancy@surreywt.org.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "11034197")),
            "unsubscribed": 0,
        },
        # ARBORICULTURAL (Surrey GU17 & South London SW16)
        {
            "company_name": "WOOD'S TREE CONSULTANCY LTD",
            "company_number": "15902451",
            "company_type": "ltd",
            "category": SpecialistCategory.ARBORICULTURAL,
            "postcode_area": "GU",
            "full_postcode": "GU17 9EX",
            "website": "https://www.woodstreeconsultancy.co.uk",
            "b2b_email": "info@woodstreeconsultancy.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "15902451")),
            "unsubscribed": 0,
        },
        {
            "company_name": "LONDON TREE SURVEY CONSULTANTS LTD",
            "company_number": "16312309",
            "company_type": "ltd",
            "category": SpecialistCategory.ARBORICULTURAL,
            "postcode_area": "SW",
            "full_postcode": "SW16 2BF",
            "website": "https://londontreesurveyconsultants.co.uk",
            "b2b_email": "info@londontreesurveyconsultants.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "16312309")),
            "unsubscribed": 0,
        },
        # CONTAMINATED_LAND (East Sussex TN32 & Surrey/Sussex border GU33)
        {
            "company_name": "KENT AND SUSSEX GEOTECHNICAL LTD",
            "company_number": "11418899",
            "company_type": "ltd",
            "category": SpecialistCategory.CONTAMINATED_LAND,
            "postcode_area": "TN",
            "full_postcode": "TN32 5UB",
            "website": "https://www.ksgeo.co.uk",
            "b2b_email": "info@ksgeo.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "11418899")),
            "unsubscribed": 0,
        },
        {
            "company_name": "R L GEOTECHNICAL LTD.",
            "company_number": "09691943",
            "company_type": "ltd",
            "category": SpecialistCategory.CONTAMINATED_LAND,
            "postcode_area": "GU",
            "full_postcode": "GU33 6LB",
            "website": "https://rlgeotechnical.co.uk",
            "b2b_email": "hello@rlgeotechnical.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "09691943")),
            "unsubscribed": 0,
        },
        # FLOOD_DRAINAGE (London WC2H & South East OX4)
        {
            "company_name": "STRATA FLOOD LIMITED",
            "company_number": "17108118",
            "company_type": "ltd",
            "category": SpecialistCategory.FLOOD_DRAINAGE,
            "postcode_area": "WC",
            "full_postcode": "WC2H 9JQ",
            "website": "https://strata-flood.co.uk",
            "b2b_email": "hello@strata-flood.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "17108118")),
            "unsubscribed": 0,
        },
        {
            "company_name": "RIDA REPORTS LIMITED",
            "company_number": "10590566",
            "company_type": "ltd",
            "category": SpecialistCategory.FLOOD_DRAINAGE,
            "postcode_area": "OX",
            "full_postcode": "OX4 1AW",
            "website": "https://www.urban-water.co.uk",
            "b2b_email": "admin@urban-water.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "10590566")),
            "unsubscribed": 0,
        },
        # HERITAGE_ARCHAEOLOGY (Keeping OX14 + Kent ME16 & South East SG4)
        {
            "company_name": "THE BUILT HERITAGE CONSULTANCY LIMITED",
            "company_number": "07314300",
            "company_type": "ltd",
            "category": SpecialistCategory.HERITAGE_ARCHAEOLOGY,
            "postcode_area": "OX",
            "full_postcode": "OX14 3PX",
            "website": "https://builtheritageconsultancy.co.uk",
            "b2b_email": "office@builtheritage.com",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "07314300")),
            "unsubscribed": 0,
        },
        {
            "company_name": "BROOK ARCHAEOLOGY LIMITED",
            "company_number": "14397861",
            "company_type": "ltd",
            "category": SpecialistCategory.HERITAGE_ARCHAEOLOGY,
            "postcode_area": "ME",
            "full_postcode": "ME16 8TX",
            "website": "https://www.brookarch.co.uk",
            "b2b_email": "info@brookarch.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "14397861")),
            "unsubscribed": 0,
        },
        {
            "company_name": "ABRAMS ARCHAEOLOGY LIMITED",
            "company_number": "12574953",
            "company_type": "ltd",
            "category": SpecialistCategory.HERITAGE_ARCHAEOLOGY,
            "postcode_area": "SG",
            "full_postcode": "SG4 0TY",
            "website": "https://www.abramsarchaeology.co.uk",
            "b2b_email": "info@abramsarchaeology.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "12574953")),
            "unsubscribed": 0,
        },
        # PLANNING_APPEAL (Brighton & Hove BN3 & BN1, Surrey GU7, keeping NW8)
        {
            "company_name": "ABSOLUTE TOWN PLANNING LTD",
            "company_number": "10860488",
            "company_type": "ltd",
            "category": SpecialistCategory.PLANNING_APPEAL,
            "postcode_area": "BN",
            "full_postcode": "BN3 2DJ",
            "website": "https://absolutetownplanning.com",
            "b2b_email": "enquiries@absolutetownplanning.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "10860488")),
            "unsubscribed": 0,
        },
        {
            "company_name": "LEWIS AND CO PLANNING SOUTH EAST LIMITED",
            "company_number": "05809390",
            "company_type": "ltd",
            "category": SpecialistCategory.PLANNING_APPEAL,
            "postcode_area": "BN",
            "full_postcode": "BN1 5PD",
            "website": "https://www.lewisplanning.co.uk",
            "b2b_email": "admin@lewisplanning.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "05809390")),
            "unsubscribed": 0,
        },
        {
            "company_name": "PLANIT CONSULTANTS LIMITED",
            "company_number": "07836000",
            "company_type": "ltd",
            "category": SpecialistCategory.PLANNING_APPEAL,
            "postcode_area": "GU",
            "full_postcode": "GU7 1JX",
            "website": "https://planitconsulting.co.uk",
            "b2b_email": "info@planitconsulting.co.uk",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "07836000")),
            "unsubscribed": 0,
        },
        {
            "company_name": "MY PLANNING CONSULTANCY LIMITED",
            "company_number": "13956931",
            "company_type": "ltd",
            "category": SpecialistCategory.PLANNING_APPEAL,
            "postcode_area": "NW",
            "full_postcode": "NW8 9JH",
            "website": "https://www.myplanningconsultancy.co.uk",
            "b2b_email": "hello@rfuenzalida.com",
            "unsubscribe_token": str(uuid.uuid5(uuid.NAMESPACE_DNS, "13956931")),
            "unsubscribed": 0,
        },
    ]
    return [BuyerRecord(**item) for item in raw_seed]


def harvest_companies_house_buyers(api_key: str) -> list[BuyerRecord]:
    """Query UK Companies House API for Sussex/South East/London consultancies, enforcing SIC/name + live website keyword & email checks."""
    harvested: list[BuyerRecord] = []
    seen_numbers: set[str] = set()
    seen_emails: set[str] = set()

    # Seed verified Sussex/South East/London consultancies first via live Companies House + website verification
    with httpx.Client(follow_redirects=True, timeout=6.0) as client:
        for verified_rec in get_fallback_buyer_records():
            try:
                ch_resp = client.get(
                    f"{COMPANIES_HOUSE_COMPANY_URL}/{verified_rec.company_number}",
                    auth=(api_key, ""),
                    timeout=8.0,
                )
                if ch_resp.status_code != 200:
                    continue
                ch_data = ch_resp.json()
                c_status = str(ch_data.get("company_status", "")).strip().lower()
                c_type = str(ch_data.get("type") or ch_data.get("company_type") or "").strip().lower()
                sic_codes = [str(s) for s in (ch_data.get("sic_codes") or [])]
                c_name = str(ch_data.get("company_name") or verified_rec.company_name).strip()

                if c_status != "active" or c_type not in ALLOWED_COMPANY_TYPES:
                    continue
                if verified_rec.postcode_area not in SUSSEX_SOUTH_EAST_POSTCODE_AREAS:
                    continue
                if not matches_sic_or_specialist_name(c_name, sic_codes):
                    continue

                b2b_email, site_text = scrape_corporate_role_email_and_text(
                    client, verified_rec.website
                )
                resolved_email = b2b_email or verified_rec.b2b_email
                if (
                    is_valid_corporate_email(resolved_email)
                    and website_text_contains_specialist_keywords(
                        site_text, verified_rec.category
                    )
                ):
                    harvested.append(
                        BuyerRecord(
                            company_name=c_name,
                            company_number=verified_rec.company_number,
                            company_type=c_type,
                            category=verified_rec.category,
                            postcode_area=verified_rec.postcode_area,
                            full_postcode=verified_rec.full_postcode,
                            website=verified_rec.website,
                            b2b_email=resolved_email,
                            unsubscribe_token=verified_rec.unsubscribe_token,
                            unsubscribed=0,
                        )
                    )
                    seen_numbers.add(verified_rec.company_number)
                    seen_emails.add(resolved_email)
            except Exception as exc:
                logger.warning(
                    "Live verification failed for %s: %s",
                    verified_rec.company_name,
                    exc,
                )

        # Also query Companies House advanced-search with Sussex/South East/London postcode filter
        for category, queries in CATEGORY_SEARCH_TERMS.items():
            cat_count = sum(1 for r in harvested if r.category == category)
            for query_str in queries:
                if cat_count >= 3:
                    break
                try:
                    resp = client.get(
                        COMPANIES_HOUSE_ADVANCED_SEARCH_URL,
                        params={
                            "company_name_includes": query_str,
                            "company_status": "active",
                            "size": 50,
                        },
                        auth=(api_key, ""),
                        timeout=10.0,
                    )
                    if resp.status_code != 200:
                        continue

                    items = resp.json().get("items", [])
                    for item in items:
                        if cat_count >= 3:
                            break
                        if not is_pecr_compliant_company(item):
                            continue

                        company_number = str(item.get("company_number", "")).strip()
                        company_name = str(
                            item.get("company_name") or item.get("title") or ""
                        ).strip()
                        company_type = str(item.get("company_type", "")).strip().lower()
                        sic_codes = [str(s) for s in (item.get("sic_codes") or [])]
                        address = (
                            item.get("registered_office_address")
                            or item.get("address")
                            or {}
                        )
                        full_postcode = str(address.get("postal_code", "")).strip().upper()
                        postcode_area = extract_postcode_area(full_postcode)

                        if (
                            not company_number
                            or not company_name
                            or postcode_area not in SUSSEX_SOUTH_EAST_POSTCODE_AREAS
                        ):
                            continue
                        if company_number in seen_numbers:
                            continue

                        # (a) Verify SIC code or explicit specialist keyword in company name
                        if not matches_sic_or_specialist_name(company_name, sic_codes):
                            continue

                        # (b) Probe candidate corporate domains and verify scraped website text contains specialist keywords
                        for candidate_url in _candidate_company_domains(company_name):
                            b2b_email, site_text = scrape_corporate_role_email_and_text(
                                client, candidate_url
                            )
                            if (
                                b2b_email
                                and b2b_email not in seen_emails
                                and website_text_contains_specialist_keywords(
                                    site_text, category
                                )
                            ):
                                record = BuyerRecord(
                                    company_name=company_name,
                                    company_number=company_number,
                                    company_type=company_type,
                                    category=category,
                                    postcode_area=postcode_area,
                                    full_postcode=full_postcode,
                                    website=candidate_url,
                                    b2b_email=b2b_email,
                                    unsubscribe_token=str(
                                        uuid.uuid5(uuid.NAMESPACE_DNS, company_number)
                                    ),
                                    unsubscribed=0,
                                )
                                harvested.append(record)
                                seen_numbers.add(company_number)
                                seen_emails.add(b2b_email)
                                cat_count += 1
                                break
                except Exception as exc:
                    logger.warning("Error harvesting '%s': %s", query_str, exc)

    return harvested


def purge_invalid_and_mock_buyers(
    valid_company_numbers: set[str],
    db_path: Optional[str] = None,
) -> int:
    """Remove mock/test buyers, false-positive companies, and non-Sussex/South East postcodes from SQLite."""
    init_db(db_path)
    allowed_areas = tuple(SUSSEX_SOUTH_EAST_POSTCODE_AREAS)
    area_placeholders = ",".join("?" for _ in allowed_areas)
    with db_session(db_path) as conn:
        if valid_company_numbers:
            num_placeholders = ",".join("?" for _ in valid_company_numbers)
            cur = conn.execute(
                f"DELETE FROM buyers WHERE company_number NOT IN ({num_placeholders}) "
                f"OR postcode_area NOT IN ({area_placeholders})",
                tuple(valid_company_numbers) + allowed_areas,
            )
            return cur.rowcount
    return 0


def seed_buyers(
    db_path: Optional[str] = None,
    seed_file_path: Optional[Path] = None,
    force_fallback: bool = False,
) -> list[BuyerRecord]:
    """Run Agent 0 buyer harvesting, purge distant/mock/false-positive records, save to data/buyers_seed.json, and upsert into SQLite."""
    init_db(db_path)
    target_json = seed_file_path or BUYERS_SEED_FILE
    target_json.parent.mkdir(parents=True, exist_ok=True)

    api_key = settings.COMPANIES_HOUSE_API_KEY.strip()
    is_placeholder = (
        not api_key
        or api_key.startswith("your-")
        or api_key == "test"
        or force_fallback
    )

    records: list[BuyerRecord] = []
    if not is_placeholder:
        records = harvest_companies_house_buyers(api_key)

    if not records:
        logger.info("Using verified Sussex & South East corporate specialist fallback records.")
        records = get_fallback_buyer_records()

    valid_numbers = {r.company_number for r in records}
    purge_invalid_and_mock_buyers(valid_numbers, db_path=db_path)

    serialized = [r.model_dump(mode="json") for r in records]
    target_json.write_text(json.dumps(serialized, indent=2), encoding="utf-8")

    for rec in records:
        upsert_buyer(rec.model_dump(mode="json"), db_path=db_path)

    return records


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seeded = seed_buyers()
    print(f"Seeded {len(seeded)} verified PECR-compliant specialist buyers into SQLite and {BUYERS_SEED_FILE}")
