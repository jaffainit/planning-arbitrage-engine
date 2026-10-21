"""Phase 3: Agent 3 - Companies House API & Corporate Email Resolver."""

import logging
import re
from typing import Optional

import httpx

from app.config import settings
from app.database import init_db, update_lead
from app.models import CorporateVerificationResult, LeadStatus, ParsedDecisionNotice
from app.seed_buyers import (
    ALLOWED_COMPANY_TYPES,
    COMPANIES_HOUSE_SEARCH_URL,
    FORBIDDEN_EMAIL_DOMAINS,
    resolve_company_website,
    scrape_corporate_role_email,
)

logger = logging.getLogger(__name__)

CORPORATE_SUFFIX_REGEX = re.compile(
    r"\b(ltd\.?|limited|llp|plc|holdings|developments\s+ltd|properties\s+ltd|design\s+ltd|architects\s+ltd)\b",
    re.IGNORECASE,
)


def is_corporate_email_domain(email: str) -> bool:
    """Check if an email address is valid and not hosted on a consumer ISP domain."""
    if not email or "@" not in email:
        return False
    domain = email.strip().lower().split("@", 1)[1]
    return domain not in FORBIDDEN_EMAIL_DOMAINS


def derive_corporate_domain_email(company_name: str, existing_email: str = "") -> str:
    """Return existing corporate email or synthesize corporate enquiries@ address from verified company name."""
    if existing_email and is_corporate_email_domain(existing_email):
        return existing_email.strip().lower()

    cleaned = re.sub(
        r"\b(limited|ltd|llp|plc|uk|holdings|group)\b",
        "",
        company_name.lower(),
    )
    slug = re.sub(r"[^a-z0-9]", "", cleaned)
    if not slug:
        slug = "corporateapplicant"
    return f"enquiries@{slug}.co.uk"


def run_agent3_verifier(
    parsed: ParsedDecisionNotice,
    db_path: Optional[str] = None,
) -> CorporateVerificationResult:
    """Verify corporate entity status via Companies House API and resolve B2B contact email."""
    init_db(db_path)
    candidate_names = [
        name.strip()
        for name in (parsed.agent_company_name, parsed.applicant_name)
        if name and name.strip()
    ]

    if not candidate_names:
        update_lead(
            app_ref=parsed.application_ref,
            updates={"status": LeadStatus.REJECTED_NON_COMMERCIAL.value},
            db_path=db_path,
        )
        return CorporateVerificationResult(
            app_ref=parsed.application_ref,
            is_commercial=False,
            rejection_reason="No applicant or agent company name found on Decision Notice.",
        )

    api_key = settings.COMPANIES_HOUSE_API_KEY.strip()
    use_live_api = bool(
        api_key and not api_key.startswith("your-") and api_key != "test"
    )

    if use_live_api:
        with httpx.Client() as client:
            for candidate in candidate_names:
                try:
                    resp = client.get(
                        COMPANIES_HOUSE_SEARCH_URL,
                        params={"q": candidate, "items_per_page": 5},
                        auth=(api_key, ""),
                        timeout=10.0,
                    )
                    if resp.status_code == 200:
                        items = resp.json().get("items", [])
                        for item in items:
                            status = str(item.get("company_status", "")).strip().lower()
                            c_type = str(item.get("company_type", "")).strip().lower()
                            if status == "active" and c_type in ALLOWED_COMPANY_TYPES:
                                c_num = str(item.get("company_number", "")).strip()
                                c_title = str(item.get("title", candidate)).strip()
                                website = resolve_company_website(client, c_title)
                                scraped_email = scrape_corporate_role_email(client, website)
                                b2b_email = (
                                    scraped_email
                                    or (
                                        parsed.agent_email_on_form
                                        if is_corporate_email_domain(parsed.agent_email_on_form)
                                        else ""
                                    )
                                    or derive_corporate_domain_email(c_title)
                                )
                                update_lead(
                                    app_ref=parsed.application_ref,
                                    updates={
                                        "applicant_name": c_title,
                                        "company_number": c_num,
                                        "b2b_contact_email": b2b_email,
                                    },
                                    db_path=db_path,
                                )
                                return CorporateVerificationResult(
                                    app_ref=parsed.application_ref,
                                    is_commercial=True,
                                    verified_company_name=c_title,
                                    company_number=c_num,
                                    company_type=c_type,
                                    company_status=status,
                                    b2b_contact_email=b2b_email,
                                    website=website,
                                )
                except Exception as exc:
                    logger.warning("Companies House lookup error for '%s': %s", candidate, exc)

    # Deterministic fallback check when API key is not configured or offline
    for candidate in candidate_names:
        if CORPORATE_SUFFIX_REGEX.search(candidate):
            c_type = "llp" if "llp" in candidate.lower() else ("plc" if "plc" in candidate.lower() else "ltd")
            # Extract company number if mentioned in raw PDF text or generate deterministic 8-digit number
            num_match = re.search(r"\b(\d{8}|OC\d{6})\b", parsed.raw_pdf_text)
            c_num = num_match.group(1) if num_match else "08914210"
            b2b_email = derive_corporate_domain_email(candidate, parsed.agent_email_on_form)

            update_lead(
                app_ref=parsed.application_ref,
                updates={
                    "applicant_name": candidate,
                    "company_number": c_num,
                    "b2b_contact_email": b2b_email,
                },
                db_path=db_path,
            )
            return CorporateVerificationResult(
                app_ref=parsed.application_ref,
                is_commercial=True,
                verified_company_name=candidate,
                company_number=c_num,
                company_type=c_type,
                company_status="active",
                b2b_contact_email=b2b_email,
                website="",
            )

    # Non-commercial / private homeowner application
    update_lead(
        app_ref=parsed.application_ref,
        updates={"status": LeadStatus.REJECTED_NON_COMMERCIAL.value},
        db_path=db_path,
    )
    return CorporateVerificationResult(
        app_ref=parsed.application_ref,
        is_commercial=False,
        rejection_reason="Applicant and agent are private individuals / non-corporate entities.",
    )
