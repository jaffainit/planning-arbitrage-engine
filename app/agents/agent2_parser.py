"""Phase 3: Agent 2 - PyMuPDF Extractor + Local `agy` CLI JSON Condition & Refusal Parser."""

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import pymupdf as fitz  # PyMuPDF

from app.agy_client import call_agy_json, resolve_agy_binary
from app.database import init_db, update_lead
from app.models import (
    DecisionType,
    LeadStatus,
    ParsedDecisionNotice,
    SpecialistCategory,
    SpecialistTrigger,
)

logger = logging.getLogger(__name__)

POSTCODE_REGEX = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    re.IGNORECASE,
)

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

CATEGORY_KEYWORD_PATTERNS: list[tuple[SpecialistCategory, re.Pattern[str]]] = [
    (
        SpecialistCategory.ACOUSTIC_BS4142,
        re.compile(r"\b(BS\s*4142|acoustic|noise\s+impact|sound\s+insulation|plant\s+noise)\b", re.IGNORECASE),
    ),
    (
        SpecialistCategory.ECOLOGY_BATS_BNG,
        re.compile(r"\b(bat\s+emergence|biodiversity\s+net\s+gain|ecological|protected\s+species|great\s+crested\s+newt)\b", re.IGNORECASE),
    ),
    (
        SpecialistCategory.ARBORICULTURAL,
        re.compile(r"\b(BS\s*5837|arboricultural|tree\s+protection)\b", re.IGNORECASE),
    ),
    (
        SpecialistCategory.CONTAMINATED_LAND,
        re.compile(r"\b(contaminated\s+land|geo-environmental|remediation\s+strategy|intrusive\s+site\s+investigation)\b", re.IGNORECASE),
    ),
    (
        SpecialistCategory.FLOOD_DRAINAGE,
        re.compile(r"\b(sustainable\s+drainage|SuDS|flood\s+risk\s+assessment|surface\s+water\s+drainage)\b", re.IGNORECASE),
    ),
    (
        SpecialistCategory.HERITAGE_ARCHAEOLOGY,
        re.compile(r"\b(written\s+scheme\s+of\s+investigation|archaeological|heritage\s+statement|listed\s+building)\b", re.IGNORECASE),
    ),
    (
        SpecialistCategory.PLANNING_APPEAL,
        re.compile(r"\b(hereby\s+refuse|refused\s+for\s+the\s+following\s+reason|contrary\s+to\s+policy)\b", re.IGNORECASE),
    ),
]


def extract_text_from_pdf(pdf_path: str | Path) -> str:
    """Extract full raw text from a PDF file using PyMuPDF (fitz)."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")

    pages_text: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            pages_text.append(page.get_text("text"))
    return "\n".join(pages_text).strip()


def extract_outward_postcode(postcode: str) -> str:
    """Extract outward code (e.g. 'BN1' from 'BN1 4GH') or alphabetic prefix."""
    cleaned = postcode.strip().upper()
    parts = cleaned.split()
    if len(parts) >= 2:
        return parts[0]
    if len(cleaned) >= 5:
        return cleaned[:-3].strip()
    return cleaned


def extract_postcode_area_prefix(postcode: str) -> str:
    """Extract 1-2 letter UK postcode area prefix (e.g. 'BN' from 'BN1 4GH')."""
    match = re.match(r"^([A-Z]{1,2})", postcode.strip().upper())
    return match.group(1) if match else ""


def _deterministic_parse_pdf_text(raw_text: str, fallback_app_ref: str = "") -> ParsedDecisionNotice:
    """Deterministic parser extracting statutory conditions, refusal reasons, and metadata directly from text."""
    ref_match = re.search(
        r"(?:Application\s*(?:Reference|Ref|No\.?)\s*[:\-]?\s*)([A-Z]{1,4}\d{2,4}/\d{3,6}(?:/[A-Z0-9]+)?)",
        raw_text,
        re.IGNORECASE,
    )
    if not ref_match:
        ref_match = re.search(r"\b([A-Z]{1,4}\d{2,4}/\d{3,6}(?:/[A-Z0-9]+)?)\b", raw_text)
    application_ref = ref_match.group(1).strip() if ref_match else (fallback_app_ref or "UNKNOWN_REF")

    site_match = re.search(
        r"(?:Site\s*Address|Location|Development\s*Site|Address)\s*[:\-]\s*(.+)",
        raw_text,
        re.IGNORECASE,
    )
    site_address = " ".join(site_match.group(1).split()) if site_match else "Unknown Site Address"

    pc_match = POSTCODE_REGEX.search(site_address) or POSTCODE_REGEX.search(raw_text)
    postcode = pc_match.group(1).upper().strip() if pc_match else "BN1 1AA"

    applicant_match = re.search(r"Applicant(?:\s*Name)?\s*[:\-]\s*(.+)", raw_text, re.IGNORECASE)
    applicant_name = applicant_match.group(1).strip() if applicant_match else ""

    agent_match = re.search(r"Agent(?:\s*Company|\s*Name)?\s*[:\-]\s*(.+)", raw_text, re.IGNORECASE)
    agent_company_name = agent_match.group(1).strip() if agent_match else ""

    if not agent_company_name and not applicant_name:
        # In Brighton & Hove notices, the addressee immediately follows 'Application No: BH2026/...'
        addressee_match = re.search(
            r"Application\s*No\s*:\s*[A-Z0-9/]+\s*\n\s*([^\n]+)",
            raw_text,
            re.IGNORECASE,
        )
        if addressee_match:
            candidate_line = addressee_match.group(1).strip()
            if re.search(r"\b(ltd|limited|llp|plc|planning|architects|design|consult|partners|associates)\b", candidate_line, re.IGNORECASE):
                agent_company_name = candidate_line
            else:
                applicant_name = candidate_line

    email_match = EMAIL_REGEX.search(raw_text)
    agent_email = email_match.group(0).strip() if email_match else ""

    # Determine decision type
    if re.search(r"\b(REFUSAL\s+OF\s+PLANNING\s+PERMISSION|HEREBY\s+REFUSE|REFUSE\s+PLANNING\s+PERMISSION)\b", raw_text, re.IGNORECASE):
        decision_type = DecisionType.REFUSAL
    elif re.search(
        r"\b(GRANT\s+OF\s+(?:CONDITIONAL\s+)?PLANNING\s+PERMISSION|SUBJECT\s+TO\s+(?:THE\s+FOLLOWING\s+|COMPLIANCE\s+WITH\s+ANY\s+)?CONDITION|CONDITIONAL\s+APPROVAL|Condition\s+\d+|Conditions\s*:)\b",
        raw_text,
        re.IGNORECASE,
    ):
        decision_type = DecisionType.CONDITIONAL_APPROVAL
    else:
        decision_type = DecisionType.UNCONDITIONAL_OR_IRRELEVANT

    triggers: list[SpecialistTrigger] = []

    # Split text into numbered conditions/reasons or paragraphs to preserve exact verbatim quotes
    blocks = re.split(
        r"(?=\bCondition\s+\d+\s*[:.])|(?=\bReason(?:\s+\d+)?\s*[:.])|(?=\n\s*\d{1,2}\.\s+[A-Z])|\n\s*\n",
        raw_text,
    )
    for block in blocks:
        clean_block = " ".join(block.split())
        if len(clean_block) < 25:
            continue

        cond_num_match = re.search(r"\b(Condition\s+\d+|Reason\s+\d+)\b|^(\d{1,2})\.\s+", clean_block, re.IGNORECASE)
        for category, pattern in CATEGORY_KEYWORD_PATTERNS:
            if pattern.search(clean_block):
                if cond_num_match:
                    cond_label = cond_num_match.group(1) or f"Condition {cond_num_match.group(2)}"
                else:
                    cond_label = "Condition 1"
                # Extract verbatim sentence/condition text
                quote_text = clean_block
                prefix_match = re.match(r"^(?:Condition\s+\d+|Reason\s+\d+|\d{1,2})\s*[:.\-–]?\s*(.+)$", clean_block, re.IGNORECASE)
                if prefix_match:
                    quote_text = prefix_match.group(1).strip()

                pre_comm = bool(
                    re.search(
                        r"\b(prior\s+to\s+commencement|before\s+any\s+development\s+commences|prior\s+to\s+first\s+occupation|no\s+development\s+shall\s+take\s+place)\b",
                        clean_block,
                        re.IGNORECASE,
                    )
                )
                triggers.append(
                    SpecialistTrigger(
                        category=category,
                        condition_number=cond_label,
                        verbatim_quote=quote_text,
                        pre_commencement=pre_comm,
                    )
                )
                break

    if not triggers:
        decision_type = DecisionType.UNCONDITIONAL_OR_IRRELEVANT

    return ParsedDecisionNotice(
        application_ref=application_ref,
        site_address=site_address,
        postcode=postcode,
        decision_type=decision_type,
        applicant_name=applicant_name,
        agent_company_name=agent_company_name,
        agent_email_on_form=agent_email,
        specialist_triggers=triggers,
        raw_pdf_text=raw_text,
    )


AGENT2_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "application_ref": {"type": "string"},
        "site_address": {"type": "string"},
        "postcode": {"type": "string"},
        "decision_type": {
            "type": "string",
            "enum": [
                "CONDITIONAL_APPROVAL",
                "REFUSAL",
                "UNCONDITIONAL_OR_IRRELEVANT",
            ],
        },
        "applicant_name": {"type": "string"},
        "agent_company_name": {"type": "string"},
        "agent_email_on_form": {"type": "string"},
        "specialist_triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "ACOUSTIC_BS4142",
                            "ECOLOGY_BATS_BNG",
                            "ARBORICULTURAL",
                            "CONTAMINATED_LAND",
                            "FLOOD_DRAINAGE",
                            "HERITAGE_ARCHAEOLOGY",
                            "PLANNING_APPEAL",
                        ],
                    },
                    "condition_number": {"type": "string"},
                    "verbatim_quote": {"type": "string"},
                    "pre_commencement": {"type": "boolean"},
                },
                "required": [
                    "category",
                    "condition_number",
                    "verbatim_quote",
                    "pre_commencement",
                ],
            },
        },
    },
    "required": [
        "application_ref",
        "site_address",
        "postcode",
        "decision_type",
        "applicant_name",
        "agent_company_name",
        "agent_email_on_form",
        "specialist_triggers",
    ],
}


def _llm_parse_pdf_text(raw_text: str, fallback_app_ref: str = "") -> ParsedDecisionNotice:
    """Call local `agy -p "..." --output-format json --json-schema '...'` CLI subprocess to parse the Decision Notice text."""
    prompt = (
        "You are a senior UK Chartered Town Planner and legal analyst. "
        "Do NOT call any tools or read any files; analyze ONLY the text provided below and return the JSON immediately.\n"
        "Extract structured planning decision details and statutory specialist conditions from the UK Council Decision Notice text.\n"
        "CRITICAL RULE: Every `verbatim_quote` in `specialist_triggers` MUST be copied word-for-word from the provided PDF text without altering a single character.\n"
        "Valid categories: ACOUSTIC_BS4142, ECOLOGY_BATS_BNG, ARBORICULTURAL, CONTAMINATED_LAND, FLOOD_DRAINAGE, HERITAGE_ARCHAEOLOGY, PLANNING_APPEAL.\n"
        "Valid decision_type values: CONDITIONAL_APPROVAL, REFUSAL, UNCONDITIONAL_OR_IRRELEVANT.\n\n"
        f"Application Reference hint: {fallback_app_ref}\n\n"
        f"DECISION NOTICE RAW TEXT:\n{raw_text}"
    )

    data = call_agy_json(prompt=prompt, json_schema=AGENT2_JSON_SCHEMA)
    data["raw_pdf_text"] = raw_text
    if not data.get("application_ref") and fallback_app_ref:
        data["application_ref"] = fallback_app_ref
    return ParsedDecisionNotice.model_validate(data)


def run_agent2_parser(
    pdf_path: str | Path,
    app_ref: Optional[str] = None,
    db_path: Optional[str] = None,
) -> ParsedDecisionNotice:
    """Extract text from Decision Notice PDF, parse conditions via local `agy` CLI (with deterministic fallback), and update SQLite."""
    init_db(db_path)
    raw_text = extract_text_from_pdf(pdf_path)
    fallback_ref = app_ref or Path(pdf_path).stem.replace("_", "/", 1)

    det_parsed = _deterministic_parse_pdf_text(raw_text, fallback_app_ref=fallback_ref)
    parsed: Optional[ParsedDecisionNotice] = None

    if det_parsed.specialist_triggers and resolve_agy_binary() is not None:
        try:
            parsed = _llm_parse_pdf_text(raw_text, fallback_app_ref=fallback_ref)
            logger.info("Agent 2 parsed %s via local `agy` CLI subprocess.", fallback_ref)
        except Exception as exc:
            logger.warning("Local `agy` CLI parsing failed (%s); falling back to deterministic parser.", exc)

    if parsed is None:
        parsed = det_parsed

    target_ref = app_ref or parsed.application_ref
    parsed.application_ref = target_ref
    postcode_area = extract_postcode_area_prefix(parsed.postcode)

    if (
        parsed.decision_type == DecisionType.UNCONDITIONAL_OR_IRRELEVANT
        or len(parsed.specialist_triggers) == 0
    ):
        update_lead(
            app_ref=target_ref,
            updates={
                "site_address": parsed.site_address,
                "postcode": parsed.postcode,
                "postcode_area": postcode_area,
                "decision_type": parsed.decision_type.value,
                "applicant_name": parsed.applicant_name or parsed.agent_company_name,
                "status": LeadStatus.IGNORED.value,
            },
            db_path=db_path,
        )
    else:
        primary_trigger = parsed.specialist_triggers[0]
        update_lead(
            app_ref=target_ref,
            updates={
                "site_address": parsed.site_address,
                "postcode": parsed.postcode,
                "postcode_area": postcode_area,
                "decision_type": parsed.decision_type.value,
                "category": primary_trigger.category.value,
                "verbatim_quote": primary_trigger.verbatim_quote,
                "applicant_name": parsed.agent_company_name or parsed.applicant_name,
            },
            db_path=db_path,
        )

    return parsed
