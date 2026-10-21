"""Phase 1: Pydantic v2 Schemas for Strict Inter-Agent Handoffs."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class SpecialistCategory(str, Enum):
    """7 UK Specialist Consultancy Categories."""

    ACOUSTIC_BS4142 = "ACOUSTIC_BS4142"
    ECOLOGY_BATS_BNG = "ECOLOGY_BATS_BNG"
    ARBORICULTURAL = "ARBORICULTURAL"
    CONTAMINATED_LAND = "CONTAMINATED_LAND"
    FLOOD_DRAINAGE = "FLOOD_DRAINAGE"
    HERITAGE_ARCHAEOLOGY = "HERITAGE_ARCHAEOLOGY"
    PLANNING_APPEAL = "PLANNING_APPEAL"


class DecisionType(str, Enum):
    """Planning Decision Notice Classification."""

    CONDITIONAL_APPROVAL = "CONDITIONAL_APPROVAL"
    REFUSAL = "REFUSAL"
    UNCONDITIONAL_OR_IRRELEVANT = "UNCONDITIONAL_OR_IRRELEVANT"


class LeadStatus(str, Enum):
    """SQLite Lead Lifecycle States."""

    NEW = "NEW"
    IGNORED = "IGNORED"
    REJECTED_NON_COMMERCIAL = "REJECTED_NON_COMMERCIAL"
    CRITIC_REJECTED = "CRITIC_REJECTED"
    MERGED_DUPLICATE = "MERGED_DUPLICATE"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    DISPATCHED = "DISPATCHED"
    SOLD = "SOLD"
    REFUNDED = "REFUNDED"
    DISPUTED = "DISPUTED"


class BuyerRecord(BaseModel):
    """Agent 0 Output: Verified UK Corporate Buyer."""

    model_config = ConfigDict(extra="ignore")

    company_name: str
    company_number: str
    company_type: str = Field(description="Must be ltd, llp, or plc")
    category: SpecialistCategory
    postcode_area: str
    full_postcode: str
    website: str = ""
    b2b_email: str
    unsubscribe_token: str
    unsubscribed: int = 0


class HarvestedPDF(BaseModel):
    """Agent 1 Output: Downloaded Council Decision Notice PDF."""

    model_config = ConfigDict(extra="ignore")

    app_ref: str
    pdf_path: str
    pdf_sha256: str
    source_url: str
    lead_id: Optional[int] = None


class SpecialistTrigger(BaseModel):
    """Individual statutory condition or refusal reason triggering specialist survey work."""

    model_config = ConfigDict(extra="ignore")

    category: SpecialistCategory
    condition_number: str = Field(description="Condition or refusal reason number, e.g. 'Condition 4'")
    verbatim_quote: str = Field(description="Exact word-for-word quote from the Decision Notice PDF")
    pre_commencement: bool = Field(default=True, description="True if condition must be discharged prior to commencement/occupation")


class ParsedDecisionNotice(BaseModel):
    """Agent 2 Output: Structured extraction from Decision Notice PDF."""

    model_config = ConfigDict(extra="ignore")

    application_ref: str
    site_address: str
    postcode: str
    decision_type: DecisionType
    applicant_name: str = ""
    agent_company_name: str = ""
    agent_email_on_form: str = ""
    specialist_triggers: list[SpecialistTrigger] = Field(default_factory=list)
    raw_pdf_text: str = ""


class CorporateVerificationResult(BaseModel):
    """Agent 3 Output: Companies House & PECR B2B Verification."""

    model_config = ConfigDict(extra="ignore")

    app_ref: str
    is_commercial: bool
    verified_company_name: str = ""
    company_number: str = ""
    company_type: str = ""
    company_status: str = ""
    b2b_contact_email: str = ""
    website: str = ""
    rejection_reason: Optional[str] = None


class SynthesizedDossier(BaseModel):
    """Agent 4 Output: Generated Redacted Teaser & Unredacted Full Dossier PDFs."""

    model_config = ConfigDict(extra="ignore")

    app_ref: str
    postcode_outward: str
    category: SpecialistCategory
    estimated_contract_value_gbp: str
    teaser_pdf_path: str
    dossier_pdf_path: str


class CriticEvaluation(BaseModel):
    """Agent 5 Output: Deterministic Verbatim Quote & PECR Legal Check."""

    model_config = ConfigDict(extra="ignore")

    app_ref: str
    passed: bool
    quote_similarity: float
    pecr_compliant: bool
    rejection_reason: Optional[str] = None


class CloserDispatchResult(BaseModel):
    """Agent 6 Output: Stripe Payment Link & Buyer Outreach Dispatch Summary."""

    model_config = ConfigDict(extra="ignore")

    app_ref: str
    lead_id: int
    stripe_payment_link_id: str
    stripe_payment_url: str
    status: LeadStatus
    dispatched_buyer_emails: list[str] = Field(default_factory=list)
    pending_telegram_approval: bool = False
