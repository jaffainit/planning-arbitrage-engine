"""Phase 3: Agent 4 - ReportLab PDF Dossier Builder + Local `agy` CLI Dossier Copy Synthesizer."""

import logging
from pathlib import Path
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.agents.agent1_harvester import sanitize_app_ref
from app.agents.agent2_parser import extract_outward_postcode
from app.agy_client import call_agy_json, resolve_agy_binary
from app.config import DOSSIERS_DIR
from app.database import init_db, update_lead
from app.models import (
    CorporateVerificationResult,
    ParsedDecisionNotice,
    SpecialistCategory,
    SynthesizedDossier,
)

logger = logging.getLogger(__name__)

ESTIMATED_CONTRACT_VALUES: dict[SpecialistCategory, str] = {
    SpecialistCategory.ACOUSTIC_BS4142: "£1,800 - £3,500 + VAT",
    SpecialistCategory.ECOLOGY_BATS_BNG: "£2,200 - £4,800 + VAT",
    SpecialistCategory.ARBORICULTURAL: "£1,200 - £2,600 + VAT",
    SpecialistCategory.CONTAMINATED_LAND: "£3,500 - £8,500 + VAT",
    SpecialistCategory.FLOOD_DRAINAGE: "£2,000 - £4,500 + VAT",
    SpecialistCategory.HERITAGE_ARCHAEOLOGY: "£2,500 - £6,000 + VAT",
    SpecialistCategory.PLANNING_APPEAL: "£4,000 - £12,000 + VAT",
}

AGENT4_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "estimated_contract_value_gbp": {"type": "string"},
        "teaser_executive_summary": {"type": "string"},
        "outreach_subject": {"type": "string"},
        "outreach_body_html": {"type": "string"},
    },
    "required": [
        "estimated_contract_value_gbp",
        "teaser_executive_summary",
        "outreach_subject",
        "outreach_body_html",
    ],
}


def _synthesize_copy_via_agy(
    parsed: ParsedDecisionNotice,
    verification: CorporateVerificationResult,
    category: SpecialistCategory,
    condition_number: str,
    verbatim_quote: str,
    default_value: str,
) -> dict[str, str]:
    """Use local `agy -p "..." --output-format json --json-schema '...'` subprocess to craft bespoke dossier copy."""
    fallback = {
        "estimated_contract_value_gbp": default_value,
        "teaser_executive_summary": (
            "This brief confirms a newly issued UK Council Decision Notice requiring mandatory specialist consultancy discharge. "
            "Full site address, application reference, and corporate contact details are unlocked immediately upon acquisition."
        ),
        "outreach_subject": f"Discharge of {condition_number} ({category.value}) — {parsed.site_address} ({parsed.application_ref})",
        "outreach_body_html": (
            f"Dear {verification.verified_company_name} Team,<br/><br/>"
            f"Congratulations on the recent decision notice issued for {parsed.site_address} (Ref: {parsed.application_ref}). "
            f"We noted that {condition_number} requires a specialist {category.value} assessment prior to discharge.<br/><br/>"
            f"Our practice regularly prepares council-compliant {category.value} reports in the {parsed.postcode} area "
            f"with fast turnaround times to prevent any pre-commencement delays. "
            f"Would you like us to send across a fixed-fee proposal today?<br/><br/>"
            f"Kind regards,<br/>[Your Practice Name]"
        ),
    }

    if resolve_agy_binary() is None:
        return fallback

    prompt = (
        "You are a UK B2B Planning Intelligence Synthesizer. "
        "Do NOT call any tools or read any files; return the JSON immediately.\n"
        f"Application Ref: {parsed.application_ref}\n"
        f"Site Address: {parsed.site_address} ({parsed.postcode})\n"
        f"Specialist Category: {category.value}\n"
        f"Condition Number: {condition_number}\n"
        f"Verbatim Condition: {verbatim_quote}\n"
        f"Verified Applicant/Architect Company: {verification.verified_company_name} ({verification.company_number})\n"
        f"Default Estimated Value Range: {default_value}\n\n"
        "Generate:\n"
        "1. `estimated_contract_value_gbp`: Realistic UK consultancy fee range (e.g. '£1,800 - £3,500 + VAT').\n"
        "2. `teaser_executive_summary`: 2-sentence redacted summary for the Teaser PDF (MUST NOT mention the street address, application_ref, or company name).\n"
        "3. `outreach_subject`: Compelling B2B email subject line referencing the application reference and condition.\n"
        "4. `outreach_body_html`: Ready-to-send B2B outreach email body using `<br/>` line breaks addressed to the verified company."
    )

    try:
        res = call_agy_json(prompt=prompt, json_schema=AGENT4_JSON_SCHEMA)
        logger.info("Agent 4 synthesized dossier copy for %s via local `agy` CLI subprocess.", parsed.application_ref)
        return {
            "estimated_contract_value_gbp": str(res.get("estimated_contract_value_gbp") or default_value),
            "teaser_executive_summary": str(res.get("teaser_executive_summary") or fallback["teaser_executive_summary"]),
            "outreach_subject": str(res.get("outreach_subject") or fallback["outreach_subject"]),
            "outreach_body_html": str(res.get("outreach_body_html") or fallback["outreach_body_html"]),
        }
    except Exception as exc:
        logger.warning("Local `agy` CLI synthesis in Agent 4 failed (%s); using deterministic template.", exc)
        return fallback


def _build_teaser_pdf(
    output_path: Path,
    postcode_outward: str,
    category: SpecialistCategory,
    condition_number: str,
    verbatim_quote: str,
    company_type: str,
    estimated_value: str,
    executive_summary: str,
) -> None:
    """Generate Redacted Teaser PDF showing outward postcode, verbatim condition, and active corporate status."""
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TeaserTitle",
        parent=styles["Heading1"],
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#0F172A"),
        spaceAfter=10,
    )
    sub_style = ParagraphStyle(
        "TeaserSub",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#334155"),
        spaceAfter=8,
    )
    quote_style = ParagraphStyle(
        "TeaserQuote",
        parent=styles["Normal"],
        fontSize=10,
        leading=15,
        leftIndent=10,
        rightIndent=10,
        backColor=colors.HexColor("#F8FAFC"),
        borderPadding=8,
        textColor=colors.HexColor("#1E293B"),
        spaceAfter=12,
    )

    story = [
        Paragraph("REDACTED STATUTORY PLANNING CONDITION BRIEF (TEASER)", title_style),
        Paragraph(executive_summary, sub_style),
        Spacer(1, 4 * mm),
    ]

    summary_rows = [
        ["Postcode Outward Area", postcode_outward],
        ["Specialist Discipline Required", category.value],
        ["Trigger Condition Reference", condition_number],
        ["Estimated Survey Contract Value", estimated_value],
        [
            "Applicant / Architect Corporate Status",
            f"Verified Active UK {company_type.upper()} (Companies House Confirmed)",
        ],
        ["Exact Site Street Address", "[REDACTED - UNLOCKED IN FULL DOSSIER]"],
        ["Planning Application Reference", "[REDACTED - UNLOCKED IN FULL DOSSIER]"],
        ["Decision Maker & Corporate Email", "[REDACTED - UNLOCKED IN FULL DOSSIER]"],
    ]

    table = Table(summary_rows, colWidths=[65 * mm, 105 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#0F172A")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#64748B")),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("<b>Verbatim Statutory Condition / Refusal Trigger:</b>", sub_style))
    story.append(Paragraph(f'"{verbatim_quote}"', quote_style))
    story.append(
        Paragraph(
            "Single-buyer exclusivity guarantee: The Stripe checkout link accompanying this brief is restricted "
            "to 1 completed purchase session. Once acquired, the link automatically deactivates.",
            sub_style,
        )
    )

    doc.build(story)


def _build_full_dossier_pdf(
    output_path: Path,
    parsed: ParsedDecisionNotice,
    verification: CorporateVerificationResult,
    category: SpecialistCategory,
    condition_number: str,
    verbatim_quote: str,
    estimated_value: str,
    outreach_subject: str,
    outreach_body_html: str,
) -> None:
    """Generate Unredacted Full Dossier PDF with full application ref, site address, company number, B2B email, and outreach template."""
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DossierTitle",
        parent=styles["Heading1"],
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#0F172A"),
        spaceAfter=10,
    )
    sub_style = ParagraphStyle(
        "DossierSub",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#1E293B"),
        spaceAfter=8,
    )
    box_style = ParagraphStyle(
        "DossierBox",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=14,
        leftIndent=8,
        rightIndent=8,
        backColor=colors.HexColor("#F8FAFC"),
        borderPadding=8,
        textColor=colors.HexColor("#0F172A"),
        spaceAfter=10,
    )

    story = [
        Paragraph(f"UNREDACTED PLANNING INTELLIGENCE DOSSIER: {parsed.application_ref}", title_style),
        Paragraph(
            "Confidential B2B Project Intelligence — Unredacted Statutory Condition & Corporate Contact Dossier.",
            sub_style,
        ),
        Spacer(1, 4 * mm),
    ]

    details_rows = [
        ["Planning Application Reference", parsed.application_ref],
        ["Full Development Site Address", parsed.site_address],
        ["Site Postcode", parsed.postcode],
        ["Decision Classification", parsed.decision_type.value],
        ["Required Specialist Category", category.value],
        ["Condition / Reason Number", condition_number],
        ["Estimated Survey Contract Value", estimated_value],
        ["Verified Corporate Entity", verification.verified_company_name],
        ["UK Companies House Number", verification.company_number],
        ["Corporate Entity Type & Status", f"{verification.company_type.upper()} ({verification.company_status.upper()})"],
        ["Verified Corporate B2B Email", verification.b2b_contact_email],
    ]

    table = Table(details_rows, colWidths=[65 * mm, 105 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E2E8F0")),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#0F172A")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#475569")),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("<b>Verbatim Council Condition / Refusal Text:</b>", sub_style))
    story.append(Paragraph(f'"{verbatim_quote}"', box_style))

    outreach_template = (
        f"<b>To:</b> {verification.b2b_contact_email}<br/>"
        f"<b>Subject:</b> {outreach_subject}<br/><br/>"
        f"{outreach_body_html}"
    )

    story.append(Paragraph("<b>Ready-to-Send B2B Outreach Email Template:</b>", sub_style))
    story.append(Paragraph(outreach_template, box_style))

    doc.build(story)


def run_agent4_synthesizer(
    parsed: ParsedDecisionNotice,
    verification: CorporateVerificationResult,
    output_dir: Optional[Path] = None,
    db_path: Optional[str] = None,
) -> SynthesizedDossier:
    """Generate both Redacted Teaser PDF and Unredacted Full Dossier PDF in data/dossiers_generated/."""
    init_db(db_path)
    target_dir = output_dir or DOSSIERS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    base_ref = parsed.application_ref.split(" & ")[0].strip()
    safe_ref = sanitize_app_ref(base_ref)
    teaser_path = target_dir / f"teaser_{safe_ref}.pdf"
    dossier_path = target_dir / f"dossier_{safe_ref}.pdf"

    primary_trigger = parsed.specialist_triggers[0]
    category = primary_trigger.category
    condition_number = primary_trigger.condition_number
    verbatim_quote = primary_trigger.verbatim_quote
    postcode_outward = extract_outward_postcode(parsed.postcode)
    default_value = ESTIMATED_CONTRACT_VALUES.get(category, "£1,500 - £3,500 + VAT")

    synthesized_copy = _synthesize_copy_via_agy(
        parsed=parsed,
        verification=verification,
        category=category,
        condition_number=condition_number,
        verbatim_quote=verbatim_quote,
        default_value=default_value,
    )
    estimated_value = synthesized_copy["estimated_contract_value_gbp"]

    _build_teaser_pdf(
        output_path=teaser_path,
        postcode_outward=postcode_outward,
        category=category,
        condition_number=condition_number,
        verbatim_quote=verbatim_quote,
        company_type=verification.company_type or "ltd",
        estimated_value=estimated_value,
        executive_summary=synthesized_copy["teaser_executive_summary"],
    )

    _build_full_dossier_pdf(
        output_path=dossier_path,
        parsed=parsed,
        verification=verification,
        category=category,
        condition_number=condition_number,
        verbatim_quote=verbatim_quote,
        estimated_value=estimated_value,
        outreach_subject=synthesized_copy["outreach_subject"],
        outreach_body_html=synthesized_copy["outreach_body_html"],
    )

    update_lead(
        app_ref=base_ref,
        updates={
            "teaser_pdf_path": str(teaser_path.resolve()),
            "dossier_pdf_path": str(dossier_path.resolve()),
        },
        db_path=db_path,
    )

    return SynthesizedDossier(
        app_ref=parsed.application_ref,
        postcode_outward=postcode_outward,
        category=category,
        estimated_contract_value_gbp=estimated_value,
        teaser_pdf_path=str(teaser_path.resolve()),
        dossier_pdf_path=str(dossier_path.resolve()),
    )

