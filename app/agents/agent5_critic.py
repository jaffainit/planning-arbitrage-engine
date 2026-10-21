"""Phase 3: Agent 5 - Verbatim Quote Matcher, UK PECR Guardrail & Local `agy` CLI Legal Critic."""

from difflib import SequenceMatcher
import logging
from typing import Any, Optional

from app.agy_client import call_agy_json, resolve_agy_binary
from app.database import init_db, update_lead
from app.models import (
    CorporateVerificationResult,
    CriticEvaluation,
    LeadStatus,
    ParsedDecisionNotice,
)
from app.seed_buyers import ALLOWED_COMPANY_TYPES, FORBIDDEN_EMAIL_DOMAINS

logger = logging.getLogger(__name__)

MIN_QUOTE_SIMILARITY = 0.90

AGENT5_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "pecr_compliant": {"type": "boolean"},
        "rejection_reason": {"type": "string"},
    },
    "required": ["passed", "pecr_compliant", "rejection_reason"],
}


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace sequences into single spaces and lowercase for deterministic matching."""
    return " ".join(text.split()).strip().lower()


def compute_quote_similarity(verbatim_quote: str, raw_pdf_text: str) -> float:
    """Compute maximum similarity between verbatim_quote and raw_pdf_text using normalized substring & SequenceMatcher."""
    norm_quote = normalize_whitespace(verbatim_quote)
    norm_raw = normalize_whitespace(raw_pdf_text)

    if not norm_quote or not norm_raw:
        return 0.0

    # Exact normalized substring check
    if norm_quote in norm_raw:
        return 1.0

    quote_len = len(norm_quote)
    if quote_len > len(norm_raw):
        return SequenceMatcher(None, norm_quote, norm_raw).ratio()

    # Sliding window search across raw text to find best matching span
    best_ratio = 0.0
    step = max(1, quote_len // 10)
    for start in range(0, max(1, len(norm_raw) - quote_len + 1), step):
        window = norm_raw[start : start + quote_len + 20]
        ratio = SequenceMatcher(None, norm_quote, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            if best_ratio >= 0.98:
                break

    return round(best_ratio, 4)


def verify_pecr_compliance(
    email: str,
    company_type: str,
    company_status: str = "active",
) -> tuple[bool, Optional[str]]:
    """Verify UK PECR Regulation 22 compliance: active corporate entity and non-consumer ISP email domain."""
    if not email or "@" not in email:
        return False, "Missing or invalid corporate email address."

    domain = email.strip().lower().split("@", 1)[1]
    if domain in FORBIDDEN_EMAIL_DOMAINS:
        return False, f"Consumer ISP email domain '{domain}' violates UK PECR B2B guardrail."

    if company_status.strip().lower() != "active":
        return False, f"Company status '{company_status}' is not active."

    if company_type.strip().lower() not in ALLOWED_COMPANY_TYPES:
        return False, f"Company type '{company_type}' is not a corporate body (ltd/llp/plc)."

    return True, None


def _critic_audit_via_agy(
    parsed: ParsedDecisionNotice,
    verification: CorporateVerificationResult,
    similarity: float,
) -> tuple[bool, Optional[str]]:
    """Call local `agy -p "..." --output-format json --json-schema '...'` subprocess for secondary legal & commercial critic review."""
    if resolve_agy_binary() is None:
        return True, None

    primary = parsed.specialist_triggers[0]
    prompt = (
        "You are Agent 5 (UK Planning & PECR Legal Critic). "
        "Do NOT call any tools or read any files; return the JSON immediately.\n"
        "Verify that:\n"
        "1. The extracted `verbatim_quote` genuinely requires a specialist consultancy survey or planning appeal service.\n"
        "2. The target recipient is an active UK corporate entity (`ltd`, `llp`, or `plc`) and the email is not a consumer ISP address.\n\n"
        f"Application Ref: {parsed.application_ref}\n"
        f"Category: {primary.category.value}\n"
        f"Condition Number: {primary.condition_number}\n"
        f"Verbatim Quote: {primary.verbatim_quote}\n"
        f"Quote Similarity Score: {similarity}\n"
        f"Verified Company: {verification.verified_company_name} (Type: {verification.company_type}, Status: {verification.company_status})\n"
        f"B2B Email: {verification.b2b_contact_email}\n"
        "Set `passed` and `pecr_compliant` to true if valid, with `rejection_reason` as empty string."
    )
    try:
        res = call_agy_json(prompt=prompt, json_schema=AGENT5_JSON_SCHEMA)
        logger.info("Agent 5 completed `agy` CLI legal audit for %s (passed=%s).", parsed.application_ref, res.get("passed"))
        passed = bool(res.get("passed", True)) and bool(res.get("pecr_compliant", True))
        reason = str(res.get("rejection_reason") or "").strip() or None
        return passed, reason
    except Exception as exc:
        logger.warning("Local `agy` CLI audit in Agent 5 skipped (%s); relying on deterministic checks.", exc)
        return True, None


def run_agent5_critic(
    parsed: ParsedDecisionNotice,
    verification: CorporateVerificationResult,
    db_path: Optional[str] = None,
) -> CriticEvaluation:
    """Execute deterministic hallucination check (>=0.90 similarity), UK PECR check, and local `agy` CLI legal audit."""
    init_db(db_path)

    if not parsed.specialist_triggers:
        update_lead(
            app_ref=parsed.application_ref,
            updates={"status": LeadStatus.CRITIC_REJECTED.value},
            db_path=db_path,
        )
        return CriticEvaluation(
            app_ref=parsed.application_ref,
            passed=False,
            quote_similarity=0.0,
            pecr_compliant=False,
            rejection_reason="No specialist triggers present to verify.",
        )

    primary_quote = parsed.specialist_triggers[0].verbatim_quote
    similarity = compute_quote_similarity(primary_quote, parsed.raw_pdf_text)

    if similarity < MIN_QUOTE_SIMILARITY:
        reason = (
            f"Hallucination guardrail triggered: verbatim_quote similarity {similarity:.2f} "
            f"is below minimum threshold {MIN_QUOTE_SIMILARITY:.2f}."
        )
        update_lead(
            app_ref=parsed.application_ref,
            updates={"status": LeadStatus.CRITIC_REJECTED.value},
            db_path=db_path,
        )
        return CriticEvaluation(
            app_ref=parsed.application_ref,
            passed=False,
            quote_similarity=similarity,
            pecr_compliant=False,
            rejection_reason=reason,
        )

    pecr_ok, pecr_reason = verify_pecr_compliance(
        email=verification.b2b_contact_email,
        company_type=verification.company_type,
        company_status=verification.company_status or "active",
    )

    if not pecr_ok:
        update_lead(
            app_ref=parsed.application_ref,
            updates={"status": LeadStatus.CRITIC_REJECTED.value},
            db_path=db_path,
        )
        return CriticEvaluation(
            app_ref=parsed.application_ref,
            passed=False,
            quote_similarity=similarity,
            pecr_compliant=False,
            rejection_reason=pecr_reason,
        )

    agy_passed, agy_reason = _critic_audit_via_agy(
        parsed=parsed,
        verification=verification,
        similarity=similarity,
    )
    if not agy_passed:
        update_lead(
            app_ref=parsed.application_ref,
            updates={"status": LeadStatus.CRITIC_REJECTED.value},
            db_path=db_path,
        )
        return CriticEvaluation(
            app_ref=parsed.application_ref,
            passed=False,
            quote_similarity=similarity,
            pecr_compliant=False,
            rejection_reason=agy_reason or "Rejected by Agent 5 agy CLI audit.",
        )

    return CriticEvaluation(
        app_ref=parsed.application_ref,
        passed=True,
        quote_similarity=similarity,
        pecr_compliant=True,
        rejection_reason=None,
    )

