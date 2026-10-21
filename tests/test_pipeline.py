"""Phase 5: End-to-End Pytest Suite for All Agents (0 to 6) & FastAPI Webhooks."""

from pathlib import Path
from fastapi.testclient import TestClient
import pymupdf as fitz
import pytest

from app.agents.agent1_harvester import register_local_pdf
from app.agents.agent2_parser import run_agent2_parser
from app.agents.agent3_verifier import run_agent3_verifier
from app.agents.agent4_synthesizer import run_agent4_synthesizer
from app.agents.agent5_critic import run_agent5_critic
from app.agents.agent6_closer import run_agent6_closer
from app.config import settings
from app.database import db_session, get_lead_by_app_ref, init_db
from app.main import app
from app.models import (
    CorporateVerificationResult,
    DecisionType,
    LeadStatus,
    ParsedDecisionNotice,
    SpecialistCategory,
    SpecialistTrigger,
)
from app.orchestrator import process_single_pdf
from app.seed_buyers import seed_buyers
from tests.create_mock_pdf import (
    MOCK_APP_REF,
    MOCK_VERBATIM_CONDITION,
    create_mock_decision_notice_pdf,
)


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated SQLite database, mock PDF, and dossier output directory for tests."""
    test_db = str(tmp_path / "test_engine.db")
    test_seed_json = tmp_path / "buyers_seed.json"
    test_pdfs_dir = tmp_path / "pdfs_raw"
    test_dossiers_dir = tmp_path / "dossiers_generated"

    monkeypatch.setattr(settings, "DATABASE_PATH", test_db)
    monkeypatch.setattr(settings, "LEAD_PRICE_GBP", 95)
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "COMPANIES_HOUSE_API_KEY", "")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    monkeypatch.setattr(settings, "AUTO_APPROVE_MODE", True)

    init_db(test_db)
    pdf_path = create_mock_decision_notice_pdf(test_pdfs_dir / "BH2026_01842.pdf")

    return {
        "db_path": test_db,
        "seed_json": test_seed_json,
        "pdf_path": pdf_path,
        "dossiers_dir": test_dossiers_dir,
    }


def test_phase1_and_phase2_db_init_and_agent0_buyer_seeding(isolated_env: dict):
    """Test SQLite WAL initialization and Agent 0 PECR-compliant buyer seeding."""
    db_path = isolated_env["db_path"]
    seed_json = isolated_env["seed_json"]

    records = seed_buyers(db_path=db_path, seed_file_path=seed_json, force_fallback=True)
    assert len(records) >= 7
    assert seed_json.exists()

    with db_session(db_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        rows = conn.execute("SELECT * FROM buyers").fetchall()
        assert len(rows) == len(records)
        for row in rows:
            assert row["company_type"] in ("ltd", "llp", "plc")
            assert not row["b2b_email"].endswith("@gmail.com")
            assert row["unsubscribed"] == 0


def test_agent2_parser_on_mock_pdf(isolated_env: dict):
    """Test Agent 2 extracts application_ref, BS 4142 acoustic trigger, verbatim quote, and South Coast Design Ltd."""
    db_path = isolated_env["db_path"]
    pdf_path = isolated_env["pdf_path"]

    register_local_pdf(app_ref=MOCK_APP_REF, pdf_path=pdf_path, db_path=db_path)
    parsed = run_agent2_parser(pdf_path=pdf_path, app_ref=MOCK_APP_REF, db_path=db_path)

    assert parsed.application_ref == MOCK_APP_REF
    assert parsed.decision_type == DecisionType.CONDITIONAL_APPROVAL
    assert "BN1 2LA" in parsed.postcode
    assert "South Coast Design Ltd" in parsed.agent_company_name
    assert len(parsed.specialist_triggers) >= 1

    trigger = parsed.specialist_triggers[0]
    assert trigger.category == SpecialistCategory.ACOUSTIC_BS4142
    assert trigger.pre_commencement is True
    assert "BS 4142:2014+A1:2019" in trigger.verbatim_quote


def test_agent3_and_agent4_pdf_generation(isolated_env: dict):
    """Test Agent 3 corporate verification and Agent 4 generation of teaser_BH2026_01842.pdf & dossier_BH2026_01842.pdf."""
    db_path = isolated_env["db_path"]
    pdf_path = isolated_env["pdf_path"]
    dossiers_dir = isolated_env["dossiers_dir"]

    register_local_pdf(app_ref=MOCK_APP_REF, pdf_path=pdf_path, db_path=db_path)
    parsed = run_agent2_parser(pdf_path=pdf_path, app_ref=MOCK_APP_REF, db_path=db_path)
    verification = run_agent3_verifier(parsed=parsed, db_path=db_path)

    assert verification.is_commercial is True
    assert verification.company_type == "ltd"
    assert verification.b2b_contact_email == "studio@southcoastdesign.co.uk"

    synthesized = run_agent4_synthesizer(
        parsed=parsed,
        verification=verification,
        output_dir=dossiers_dir,
        db_path=db_path,
    )

    teaser_file = Path(synthesized.teaser_pdf_path)
    dossier_file = Path(synthesized.dossier_pdf_path)

    assert teaser_file.name == "teaser_BH2026_01842.pdf"
    assert dossier_file.name == "dossier_BH2026_01842.pdf"
    assert teaser_file.exists() and teaser_file.stat().st_size > 500
    assert dossier_file.exists() and dossier_file.stat().st_size > 500

    # Confirm Teaser redacts street address and application ref while Dossier includes them
    with fitz.open(teaser_file) as doc:
        teaser_text = "\n".join(page.get_text("text") for page in doc)
    assert "BN1" in teaser_text
    assert "ACOUSTIC_BS4142" in teaser_text
    assert "142 Western Road" not in teaser_text

    with fitz.open(dossier_file) as doc:
        dossier_text = "\n".join(page.get_text("text") for page in doc)
    assert "142 Western Road" in dossier_text
    assert MOCK_APP_REF in dossier_text
    assert "studio@southcoastdesign.co.uk" in dossier_text


def test_agent5_critic_verbatim_and_pecr_guardrails(isolated_env: dict):
    """Test Agent 5 Critic passes valid verbatim quotes and rejects hallucinated quotes or @gmail.com emails."""
    db_path = isolated_env["db_path"]
    pdf_path = isolated_env["pdf_path"]

    register_local_pdf(app_ref=MOCK_APP_REF, pdf_path=pdf_path, db_path=db_path)
    parsed = run_agent2_parser(pdf_path=pdf_path, app_ref=MOCK_APP_REF, db_path=db_path)
    valid_verification = run_agent3_verifier(parsed=parsed, db_path=db_path)

    # 1. Valid quote & valid corporate email -> PASS
    eval_pass = run_agent5_critic(parsed=parsed, verification=valid_verification, db_path=db_path)
    assert eval_pass.passed is True
    assert eval_pass.quote_similarity >= 0.95
    assert eval_pass.pecr_compliant is True

    # 2. Hallucinated quote -> REJECT
    hallucinated_parsed = ParsedDecisionNotice(
        application_ref=MOCK_APP_REF,
        site_address=parsed.site_address,
        postcode=parsed.postcode,
        decision_type=DecisionType.CONDITIONAL_APPROVAL,
        applicant_name="South Coast Design Ltd",
        agent_company_name="South Coast Design Ltd",
        specialist_triggers=[
            SpecialistTrigger(
                category=SpecialistCategory.ACOUSTIC_BS4142,
                condition_number="Condition 99",
                verbatim_quote="This completely fabricated archaeology and bat mitigation sentence never appeared in the council notice.",
                pre_commencement=True,
            )
        ],
        raw_pdf_text=parsed.raw_pdf_text,
    )
    eval_hallucinated = run_agent5_critic(
        parsed=hallucinated_parsed,
        verification=valid_verification,
        db_path=db_path,
    )
    assert eval_hallucinated.passed is False
    assert eval_hallucinated.quote_similarity < 0.90
    lead_after_hallucination = get_lead_by_app_ref(MOCK_APP_REF, db_path=db_path)
    assert lead_after_hallucination is not None
    assert lead_after_hallucination["status"] == LeadStatus.CRITIC_REJECTED.value

    # 3. Consumer ISP email (@gmail.com) -> REJECT under UK PECR guardrail
    gmail_verification = CorporateVerificationResult(
        app_ref=MOCK_APP_REF,
        is_commercial=True,
        verified_company_name="South Coast Design Ltd",
        company_number="08914210",
        company_type="ltd",
        company_status="active",
        b2b_contact_email="personal.architect@gmail.com",
    )
    eval_gmail = run_agent5_critic(
        parsed=parsed,
        verification=gmail_verification,
        db_path=db_path,
    )
    assert eval_gmail.passed is False
    assert eval_gmail.pecr_compliant is False
    assert "gmail.com" in (eval_gmail.rejection_reason or "")


def test_full_orchestrator_and_fastapi_endpoints(isolated_env: dict):
    """Test end-to-end orchestrator, /health, /unsubscribe/{token}, and /webhook/stripe."""
    db_path = isolated_env["db_path"]
    pdf_path = isolated_env["pdf_path"]
    dossiers_dir = isolated_env["dossiers_dir"]

    buyers = seed_buyers(
        db_path=db_path,
        seed_file_path=isolated_env["seed_json"],
        force_fallback=True,
    )

    lead = process_single_pdf(
        pdf_path=pdf_path,
        app_ref=MOCK_APP_REF,
        auto_approve=True,
        db_path=db_path,
        dossiers_dir=dossiers_dir,
    )
    assert lead["status"] == LeadStatus.DISPATCHED.value
    assert lead["stripe_payment_url"].startswith("https://buy.stripe.com/")

    with TestClient(app) as client:
        # 0. Test GET / institutional landing page
        landing_resp = client.get("/")
        assert landing_resp.status_code == 200
        assert "🟢 Live UK Local Authority Monitoring Active" in landing_resp.text
        assert "Explore Live Intelligence Feed" in landing_resp.text
        assert "Consultancy Access" in landing_resp.text
        assert "Automated Idox Ingestion" in landing_resp.text
        assert "Companies House &amp; AI Statutory Parser" in landing_resp.text
        assert "Single-Buyer £95 Stripe Lock" in landing_resp.text
        assert "S. Howarth trading as Statutory Intel" in landing_resp.text

        # 1. Test /health endpoint
        health_resp = client.get("/health")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        assert health_data["status"] == "ok"
        assert health_data["total_leads"] >= 1
        assert health_data["total_buyers"] == len(buyers)

        # 2. Test /unsubscribe/{token} endpoint
        target_buyer = buyers[0]
        unsub_resp = client.get(f"/unsubscribe/{target_buyer.unsubscribe_token}")
        assert unsub_resp.status_code == 200
        assert "Unsubscribe Confirmed" in unsub_resp.text
        assert target_buyer.b2b_email in unsub_resp.text

        with db_session(db_path) as conn:
            row = conn.execute(
                "SELECT unsubscribed FROM buyers WHERE unsubscribe_token = ?",
                (target_buyer.unsubscribe_token,),
            ).fetchone()
            assert row["unsubscribed"] == 1

        # 3. Test /webhook/stripe endpoint:
        # 3a. Delayed-notification payment method where payment_status == "unpaid" must NOT fulfill
        unpaid_payload = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "payment_status": "unpaid",
                    "payment_link": lead["stripe_payment_link_id"],
                    "metadata": {
                        "lead_id": str(lead["id"]),
                        "app_ref": MOCK_APP_REF,
                    },
                    "customer_details": {
                        "email": "enquiries@sussexacoustics.co.uk",
                    },
                }
            },
        }
        unpaid_resp = client.post("/webhook/stripe", json=unpaid_payload)
        assert unpaid_resp.status_code == 200
        assert unpaid_resp.json()["status"] == "awaiting_async_payment"
        unpaid_lead = get_lead_by_app_ref(MOCK_APP_REF, db_path=db_path)
        assert unpaid_lead is not None
        assert unpaid_lead["status"] == LeadStatus.DISPATCHED.value

        # 3b. Paid checkout session (or async_payment_succeeded / invoice.paid) marks lead SOLD
        stripe_payload = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "payment_status": "paid",
                    "payment_link": lead["stripe_payment_link_id"],
                    "metadata": {
                        "lead_id": str(lead["id"]),
                        "app_ref": MOCK_APP_REF,
                    },
                    "customer_details": {
                        "email": "enquiries@sussexacoustics.co.uk",
                    },
                }
            },
        }
        webhook_resp = client.post("/webhook/stripe", json=stripe_payload)
        assert webhook_resp.status_code == 200
        assert webhook_resp.json()["status"] == "sold"
        assert webhook_resp.json()["idempotent_replay"] is False

        sold_lead = get_lead_by_app_ref(MOCK_APP_REF, db_path=db_path)
        assert sold_lead is not None
        assert sold_lead["status"] == LeadStatus.SOLD.value

        # 3c. Idempotent replay of invoice.paid for the same lead does not re-send duplicate emails
        invoice_paid_payload = {
            "type": "invoice.paid",
            "data": {
                "object": {
                    "metadata": {
                        "lead_id": str(lead["id"]),
                        "app_ref": MOCK_APP_REF,
                    },
                    "customer_email": "enquiries@sussexacoustics.co.uk",
                    "hosted_invoice_url": "https://invoice.stripe.com/i/test_123",
                    "invoice_pdf": "https://pay.stripe.com/invoice/test_123/pdf",
                }
            },
        }
        inv_resp = client.post("/webhook/stripe", json=invoice_paid_payload)
        assert inv_resp.status_code == 200
        assert inv_resp.json()["status"] == "sold"
        assert inv_resp.json()["idempotent_replay"] is True

        # 3d. v2.core.event_destination thin ping event returns pong
        v2_ping_payload = {
            "id": "evt_test_61RM8ltWcTW4mbsxf16RJyfa2xSQLHJJh1sxm7H0KVT6",
            "object": "v2.core.event",
            "type": "v2.core.event_destination.ping",
            "created": "2026-10-21T00:00:00.000Z",
            "livemode": False,
            "related_object": {
                "id": "ed_test_61RM8ltWcTW4mbsxf16RJyfa2xSQLHJJh1sxm7H0KVT6",
                "type": "v2.core.event_destination",
                "url": "/v2/core/event_destinations/ed_test_61RM8ltWcTW4mbsxf16RJyfa2xSQLHJJh1sxm7H0KVT6",
            },
        }
        ping_resp = client.post("/webhook/stripe", json=v2_ping_payload)
        assert ping_resp.status_code == 200
        assert ping_resp.json()["status"] == "pong"
