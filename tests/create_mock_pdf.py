"""Phase 5: Generate realistic Brighton & Hove Council Decision Notice PDF (BH2026/01842)."""

from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.config import PDFS_RAW_DIR

MOCK_APP_REF = "BH2026/01842"
MOCK_VERBATIM_CONDITION = (
    "Prior to commencement of the development hereby permitted, a noise impact assessment "
    "prepared in accordance with BS 4142:2014+A1:2019 shall be submitted to and approved in "
    "writing by the Local Planning Authority."
)


def create_mock_decision_notice_pdf(output_path: Optional[Path] = None) -> Path:
    """Create a realistic Brighton & Hove Council Decision Notice PDF for BH2026/01842."""
    target = output_path or (PDFS_RAW_DIR / "BH2026_01842.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    heading = styles["Heading1"]

    story = [
        Paragraph("BRIGHTON & HOVE CITY COUNCIL", heading),
        Paragraph("TOWN AND COUNTRY PLANNING ACT 1990", normal),
        Spacer(1, 5 * mm),
        Paragraph("GRANT OF CONDITIONAL PLANNING PERMISSION", heading),
        Spacer(1, 4 * mm),
        Paragraph(f"Application Reference: {MOCK_APP_REF}", normal),
        Paragraph("Site Address: 142 Western Road, Brighton, BN1 2LA", normal),
        Paragraph("Applicant Name: Albion Commercial Developments Ltd", normal),
        Paragraph("Agent Company: South Coast Design Ltd", normal),
        Paragraph("Agent Contact Email: studio@southcoastdesign.co.uk", normal),
        Paragraph("Companies House Reg: 08914210", normal),
        Spacer(1, 6 * mm),
        Paragraph(
            "In pursuance of their powers under the above-mentioned Act, the Council "
            "hereby GRANT permission for the development subject to Compliance with the following conditions:",
            normal,
        ),
        Spacer(1, 4 * mm),
        Paragraph(
            "Condition 1: The development hereby permitted shall be Plannings commenced before "
            "the expiration of three years from the date of this permission.",
            normal,
        ),
        Spacer(1, 4 * mm),
        Paragraph(f"Condition 4: {MOCK_VERBATIM_CONDITION}", normal),
        Spacer(1, 4 * mm),
        Paragraph(
            "Reason: To safeguard the amenities of neighbouring occupiers from commercial plant noise "
            "in accordance with Policy DM20 of the Brighton & Hove City Plan Part Two.",
            normal,
        ),
    ]

    doc.build(story)
    return target


if __name__ == "__main__":
    generated = create_mock_decision_notice_pdf()
    print(f"Generated mock Council Decision Notice PDF at: {generated}")
