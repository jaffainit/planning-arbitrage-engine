"""Phase 3: Agent 6 - Stripe Single-Use Payment Link + Resend Outreach + Telegram Gate."""

import base64
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from stripe import StripeClient

from app.agents.agent2_parser import extract_outward_postcode, extract_postcode_area_prefix
from app.agents.agent4_synthesizer import run_agent4_synthesizer
from app.config import settings
from app.database import (
    find_duplicate_primary_lead,
    get_lead_by_app_ref,
    get_matching_buyers,
    init_db,
    record_dispatch,
    update_lead,
)
from app.models import (
    CloserDispatchResult,
    CorporateVerificationResult,
    DecisionType,
    LeadStatus,
    ParsedDecisionNotice,
    SpecialistCategory,
    SpecialistTrigger,
    SynthesizedDossier,
)

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
STRIPE_API_VERSION = "2026-09-30.endive"
DEFAULT_STRIPE_ENABLED_EVENTS: list[str] = [
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "invoice.paid",
    "invoice.payment_failed",
    "charge.refunded",
    "credit_note.created",
    "charge.dispute.created",
    "radar.early_fraud_warning.created",
]


def get_stripe_client() -> Optional[StripeClient]:
    """Instantiate a StripeClient pinned to API version 2026-09-30.endive when a valid key (rk_ or sk_) is configured."""
    secret_key = settings.STRIPE_SECRET_KEY.strip()
    is_valid_key = bool(
        secret_key
        and not secret_key.startswith("sk_live_your_")
        and not secret_key.startswith("rk_live_your_")
        and not secret_key.startswith("your-")
        and secret_key != "test"
    )
    if not is_valid_key:
        return None
    api_version = getattr(settings, "STRIPE_API_VERSION", STRIPE_API_VERSION) or STRIPE_API_VERSION
    return StripeClient(secret_key, stripe_version=api_version)


def ensure_stripe_event_destination(
    webhook_url: Optional[str] = None,
    event_payload: str = "snapshot",
    enabled_events: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Ensure an enabled v2.core.event_destination webhook_endpoint exists for the configured PUBLIC_BASE_URL."""
    client = get_stripe_client()
    if not client:
        return None

    target_url = (webhook_url or f"{settings.PUBLIC_BASE_URL.rstrip('/')}/webhook/stripe").strip()
    api_version = getattr(settings, "STRIPE_API_VERSION", STRIPE_API_VERSION) or STRIPE_API_VERSION
    try:
        existing_list = client.v2.core.event_destinations.list(
            params={"include": ["webhook_endpoint.url"]}
        )
        for ed in existing_list.data:
            ed_url = ed.webhook_endpoint.url if getattr(ed, "webhook_endpoint", None) else None
            if (
                ed.type == "webhook_endpoint"
                and ed.status == "enabled"
                and ed_url == target_url
                and ed.event_payload == event_payload
                and (event_payload == "thin" or ed.snapshot_api_version == api_version)
            ):
                return {
                    "id": str(ed.id),
                    "object": str(ed.object),
                    "status": str(ed.status),
                    "type": str(ed.type),
                    "event_payload": str(ed.event_payload),
                    "snapshot_api_version": ed.snapshot_api_version,
                    "url": str(ed_url),
                    "signing_secret": getattr(ed.webhook_endpoint, "signing_secret", None),
                }

        create_params: dict[str, Any] = {
            "name": f"{settings.TRADING_NAME} B2B Webhook Endpoint",
            "description": f"Delivers Checkout, Invoicing, Refund, and Dispute events to {target_url}",
            "type": "webhook_endpoint",
            "event_payload": event_payload,
            "events_from": ["@self"],
            "enabled_events": enabled_events or DEFAULT_STRIPE_ENABLED_EVENTS,
            "webhook_endpoint": {"url": target_url},
            "include": ["webhook_endpoint.signing_secret", "webhook_endpoint.url"],
        }
        if event_payload == "snapshot":
            create_params["snapshot_api_version"] = api_version

        created = client.v2.core.event_destinations.create(params=create_params)  # type: ignore[arg-type]
        return {
            "id": str(created.id),
            "object": str(created.object),
            "status": str(created.status),
            "type": str(created.type),
            "event_payload": str(created.event_payload),
            "snapshot_api_version": created.snapshot_api_version,
            "url": str(created.webhook_endpoint.url if created.webhook_endpoint else target_url),
            "signing_secret": created.webhook_endpoint.signing_secret if created.webhook_endpoint else None,
        }
    except Exception as exc:
        logger.warning("Could not ensure Stripe v2.core.event_destination: %s", exc)
        return None


def deactivate_stripe_link(plink_id: str) -> bool:
    """Deactivate a Stripe Payment Link when a duplicate lead is merged or after single-buyer acquisition."""
    if not plink_id or not plink_id.startswith("plink_"):
        return False
    client = get_stripe_client()
    if not client:
        return False
    try:
        client.v1.payment_links.update(plink_id, params={"active": False})
        logger.info("Deactivated Stripe Payment Link %s", plink_id)
        return True
    except Exception as exc:
        logger.warning("Could not deactivate Stripe link %s: %s", plink_id, exc)
        return False


def create_single_use_stripe_link(
    lead_id: int,
    app_ref: str,
    category: str,
    postcode_outward: str,
) -> tuple[str, str]:
    """Create a Stripe Price (LEAD_PRICE_GBP * 100 pence) and single-session Payment Link with B2B Invoicing enabled."""
    client = get_stripe_client()

    if client:
        try:
            lead_metadata = {
                "lead_id": str(lead_id),
                "app_ref": app_ref,
                "category": category,
                "postcode_outward": postcode_outward,
            }
            price_idempotency = uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"statutoryintel:price:{lead_id}:{app_ref}:{settings.LEAD_PRICE_GBP}",
            ).hex
            link_idempotency = uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"statutoryintel:plink:{lead_id}:{app_ref}:{settings.LEAD_PRICE_GBP}",
            ).hex

            price = client.v1.prices.create(
                params={
                    "unit_amount": int(settings.LEAD_PRICE_GBP * 100),
                    "currency": "gbp",
                    "product_data": {
                        "name": f"Unredacted Planning Dossier ({category} - {postcode_outward})",
                        "metadata": lead_metadata,
                    },
                    "metadata": lead_metadata,
                },
                options={"idempotency_key": f"price_{price_idempotency}"},
            )
            plink_params: dict[str, Any] = {
                # Omit payment_method_types so Dynamic Payment Methods are enabled automatically
                "line_items": [{"price": price.id, "quantity": 1}],
                "restrictions": {"completed_sessions": {"limit": 1}},
                "customer_creation": "always",
                "tax_id_collection": {"enabled": True},
                "invoice_creation": {
                    "enabled": True,
                    "invoice_data": {
                        "description": (
                            f"Exclusive Statutory Planning Compliance Dossier — "
                            f"Ref {app_ref} ({category}, {postcode_outward})"
                        ),
                        "footer": (
                            f"{settings.SOLE_TRADER_NAME} trading as {settings.TRADING_NAME} | "
                            f"{settings.SERVICE_ADDRESS} | Single-Buyer B2B Intelligence Release"
                        ),
                        "metadata": lead_metadata,
                        "custom_fields": [
                            {"name": "Planning Ref", "value": app_ref[:30]},
                            {"name": "Sector", "value": f"{postcode_outward} ({category[:18]})"[:30]},
                        ],
                    },
                },
                "payment_intent_data": {
                    "metadata": lead_metadata,
                },
                "metadata": lead_metadata,
            }
            try:
                payment_link = client.v1.payment_links.create(
                    params={**plink_params, "managed_payments": {"enabled": False}},  # type: ignore[arg-type]
                    options={"idempotency_key": f"plink_mp0_{link_idempotency}"},
                )
            except Exception as mp_exc:
                if "managed_payments" in str(mp_exc):
                    payment_link = client.v1.payment_links.create(
                        params=plink_params,  # type: ignore[arg-type]
                        options={"idempotency_key": f"plink_{link_idempotency}"},
                    )
                else:
                    raise
            return str(payment_link.id), str(payment_link.url)
        except Exception as exc:
            logger.warning("Stripe API error (%s); generating deterministic local payment link.", exc)

    fallback_id = f"plink_{uuid.uuid5(uuid.NAMESPACE_DNS, f'{lead_id}:{app_ref}').hex[:16]}"
    fallback_url = f"https://buy.stripe.com/test_{fallback_id}"
    return fallback_id, fallback_url


def create_b2b_stripe_invoice(
    lead_id: int,
    app_ref: str,
    category: str,
    postcode_outward: str,
    buyer_email: str,
    buyer_company_name: str,
    days_until_due: int = 14,
) -> dict[str, str]:
    """Create, finalize, and return a Stripe B2B Hosted Invoice for corporate consultancies requiring PO/bank-transfer invoicing."""
    client = get_stripe_client()
    lead_metadata = {
        "lead_id": str(lead_id),
        "app_ref": app_ref,
        "category": category,
        "postcode_outward": postcode_outward,
    }

    if client:
        try:
            inv_seed = f"statutoryintel:invoice:{lead_id}:{app_ref}:{buyer_email.lower().strip()}"
            idemp_hash = uuid.uuid5(uuid.NAMESPACE_DNS, inv_seed).hex

            customer = client.v1.customers.create(
                params={
                    "email": buyer_email.strip(),
                    "name": buyer_company_name.strip(),
                    "metadata": lead_metadata,
                },
                options={"idempotency_key": f"cust_{idemp_hash}"},
            )
            invoice = client.v1.invoices.create(
                params={
                    "customer": customer.id,
                    "collection_method": "send_invoice",
                    "days_until_due": days_until_due,
                    "auto_advance": True,
                    "description": (
                        f"Exclusive Statutory Planning Compliance Dossier — "
                        f"Ref {app_ref} ({category}, {postcode_outward})"
                    ),
                    "footer": (
                        f"{settings.SOLE_TRADER_NAME} trading as {settings.TRADING_NAME} | "
                        f"{settings.SERVICE_ADDRESS} | Single-Buyer B2B Intelligence Release"
                    ),
                    "metadata": lead_metadata,
                    "custom_fields": [
                        {"name": "Planning Ref", "value": app_ref[:30]},
                        {"name": "Sector", "value": f"{postcode_outward} ({category[:18]})"[:30]},
                    ],
                },
                options={"idempotency_key": f"inv_{idemp_hash}"},
            )
            client.v1.invoice_items.create(
                params={
                    "customer": customer.id,
                    "invoice": invoice.id,
                    "amount": int(settings.LEAD_PRICE_GBP * 100),
                    "currency": "gbp",
                    "description": f"Unredacted Planning Dossier ({category} - {postcode_outward}) [Ref: {app_ref}]",
                    "metadata": lead_metadata,
                },
                options={"idempotency_key": f"ii_{idemp_hash}"},
            )
            finalized = client.v1.invoices.finalize_invoice(
                invoice.id,
                params={"auto_advance": True},
                options={"idempotency_key": f"fin_{idemp_hash}"},
            )
            return {
                "invoice_id": str(finalized.id),
                "hosted_invoice_url": str(finalized.hosted_invoice_url or ""),
                "invoice_pdf": str(finalized.invoice_pdf or ""),
                "status": str(finalized.status or "open"),
            }
        except Exception as exc:
            logger.warning("Stripe Invoicing API error (%s); returning deterministic local invoice.", exc)

    fallback_inv_id = f"in_{uuid.uuid5(uuid.NAMESPACE_DNS, f'{lead_id}:{app_ref}:{buyer_email}').hex[:16]}"
    return {
        "invoice_id": fallback_inv_id,
        "hosted_invoice_url": f"https://invoice.stripe.com/i/test_{fallback_inv_id}",
        "invoice_pdf": f"https://pay.stripe.com/invoice/test_{fallback_inv_id}/pdf",
        "status": "open",
    }


def build_pecr_sole_trader_footer(
    company_name: str = "Corporate Subscriber",
    company_number: str = "UK Registered Corporate Body",
    unsubscribe_url: Optional[str] = None,
) -> str:
    """Build mandatory UK PECR Reg 22/23, UK GDPR Art 14, and Business Names Act Sole Trader disclosure footer."""
    base_url = settings.PUBLIC_BASE_URL.rstrip("/")
    target_unsub = unsubscribe_url or f"{base_url}/unsubscribe/opt-out"
    return (
        f"<hr style='border:none;border-top:1px solid #cbd5e1;margin:20px 0;'/>"
        f"<div style='font-size:11px;line-height:1.5;color:#64748b;'>"
        f"<p style='margin:4px 0;'><strong>Sender Identity (Companies Act 2006 / Business Names):</strong> "
        f"{settings.SOLE_TRADER_NAME} trading as <strong>{settings.TRADING_NAME}</strong> | "
        f"Service Address: {settings.SERVICE_ADDRESS} | Contact: {settings.FROM_EMAIL}</p>"
        f"<p style='margin:4px 0;'><strong>UK PECR Regulations 22 &amp; 23 Notice:</strong> "
        f"This B2B planning condition intelligence alert is sent exclusively to <strong>{company_name}</strong> "
        f"(Companies House Reg No. {company_number}) as a corporate subscriber under Regulation 22 of the "
        f"Privacy and Electronic Communications (EC Directive) Regulations 2003.</p>"
        f"<p style='margin:4px 0;'><strong>UK GDPR Article 14 Notice:</strong> "
        f"Processed under Article 6(1)(f) (Legitimate Interests in B2B statutory planning condition matching). "
        f"Corporate registration and public role contact details were sourced from public statutory registers "
        f"(UK Companies House and Local Planning Authority registers).</p>"
        f"<p style='margin:6px 0;'><a href='{target_unsub}' style='color:#0f172a;font-weight:bold;'>"
        f"Instant 1-Click Unsubscribe ({target_unsub})</a></p>"
        f"</div>"
    )


def send_resend_email(
    to_email: str,
    subject: str,
    html_body: str,
    attachment_path: Optional[str] = None,
    unsubscribe_url: Optional[str] = None,
) -> str:
    """Send transactional email via Resend HTTP API with List-Unsubscribe headers and optional PDF attachment."""
    api_key = settings.RESEND_API_KEY.strip()
    is_live_key = bool(
        api_key
        and not api_key.startswith("re_your_")
        and not api_key.startswith("your-")
        and api_key != "test"
    )

    base_url = settings.PUBLIC_BASE_URL.rstrip("/")
    effective_unsub = unsubscribe_url or f"{base_url}/unsubscribe/opt-out"

    if settings.TRADING_NAME not in html_body:
        html_body = html_body + build_pecr_sole_trader_footer(unsubscribe_url=effective_unsub)

    payload: dict[str, Any] = {
        "from": f"{settings.TRADING_NAME} <{settings.FROM_EMAIL}>" if "<" not in settings.FROM_EMAIL else settings.FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "headers": {
            "List-Unsubscribe": f"<{effective_unsub}>, <mailto:{settings.FROM_EMAIL}?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }

    if attachment_path and Path(attachment_path).exists():
        pdf_bytes = Path(attachment_path).read_bytes()
        payload["attachments"] = [
            {
                "filename": Path(attachment_path).name,
                "content": base64.b64encode(pdf_bytes).decode("ascii"),
            }
        ]

    if is_live_key:
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    RESEND_API_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code in (200, 201):
                    return str(resp.json().get("id", f"re_{uuid.uuid4().hex[:12]}"))
                logger.warning("Resend API returned HTTP %s: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Resend API error sending to %s: %s", to_email, exc)

    return f"re_local_{uuid.uuid4().hex[:12]}"


def answer_telegram_callback(
    callback_query_id: str,
    text: Optional[str] = None,
) -> bool:
    """Acknowledge a Telegram inline button press via answerCallbackQuery so the loading indicator stops immediately."""
    token = settings.TELEGRAM_BOT_TOKEN.strip()
    if not callback_query_id or not token or token.startswith("your_"):
        return False

    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, json=payload)
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Telegram answerCallbackQuery failed: %s", exc)
        return False


def send_telegram_message(
    text: str,
    inline_keyboard: Optional[list[list[dict[str, str]]]] = None,
) -> bool:
    """Send Telegram notification or inline approval card via Telegram Bot API."""
    token = settings.TELEGRAM_BOT_TOKEN.strip()
    chat_id = settings.TELEGRAM_CHAT_ID.strip()
    if (
        not token
        or not chat_id
        or token.startswith("your_")
        or chat_id.startswith("your_")
    ):
        logger.info("Telegram credentials not configured; skipping live Telegram message.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Telegram sendMessage failed: %s", exc)
        return False


def send_telegram_approval_card(
    lead_row: dict[str, Any],
    db_path: Optional[str] = None,
) -> bool:
    """Format and send a rich Telegram approval card with [Approve & Dispatch] and [Discard] inline buttons."""
    lead_id = int(lead_row["id"])
    app_ref = str(lead_row.get("app_ref") or "")
    site_address = str(lead_row.get("site_address") or "")
    postcode = str(lead_row.get("postcode") or "")
    postcode_area = str(lead_row.get("postcode_area") or extract_postcode_area_prefix(postcode) or "BN")
    category = str(lead_row.get("category") or "")
    applicant_name = str(lead_row.get("applicant_name") or "")
    company_number = str(lead_row.get("company_number") or "")
    b2b_contact_email = str(lead_row.get("b2b_contact_email") or "")
    verbatim_quote = str(lead_row.get("verbatim_quote") or "").strip()

    matched_buyers = get_matching_buyers(
        category=category,
        postcode_area=postcode_area,
        exclude_company_number=company_number,
        exclude_email=b2b_contact_email,
        limit=5,
        db_path=db_path,
    )
    if matched_buyers:
        buyers_lines = "\n".join(
            f"  • {b['company_name']} ({b['full_postcode']} — <code>{b['b2b_email']}</code>)"
            for b in matched_buyers
        )
    else:
        buyers_lines = "  • None matched"

    quote_preview = verbatim_quote if len(verbatim_quote) <= 450 else verbatim_quote[:447] + "..."
    approval_text = (
        f"🏛 <b>New Verified Planning Lead Ready</b>\n\n"
        f"<b>Ref:</b> <code>{app_ref}</code>\n"
        f"<b>Site:</b> {site_address}\n"
        f"<b>Applicant/Agent:</b> {applicant_name} (CH #{company_number})\n"
        f"<b>Category:</b> <b>{category}</b>\n"
        f"<b>Condition:</b> <i>\"{quote_preview}\"</i>\n\n"
        f"<b>Matched Buyers ({len(matched_buyers)}):</b>\n{buyers_lines}\n\n"
        f"<b>Exclusivity Price:</b> £{settings.LEAD_PRICE_GBP} (Single-Use Stripe Link)"
    )
    keyboard = [
        [
            {"text": "Approve & Dispatch", "callback_data": f"approve:{lead_id}"},
            {"text": "Discard", "callback_data": f"discard:{lead_id}"},
        ]
    ]
    return send_telegram_message(text=approval_text, inline_keyboard=keyboard)


def dispatch_teaser_to_buyers(
    lead_id: int,
    app_ref: str,
    category: str,
    postcode_area: str,
    postcode_outward: str,
    verbatim_quote: str,
    teaser_pdf_path: str,
    stripe_payment_url: str,
    exclude_company_number: Optional[str] = None,
    exclude_email: Optional[str] = None,
    db_path: Optional[str] = None,
) -> list[str]:
    """Email up to 5 matching unsubscribed=0 corporate buyers (excluding the lead's own company/email) with the redacted Teaser PDF and PECR footer."""
    lead_row = get_lead_by_app_ref(app_ref, db_path=db_path)
    ex_num = (
        exclude_company_number
        if exclude_company_number is not None
        else str((lead_row or {}).get("company_number") or "")
    ).strip()
    ex_email = (
        exclude_email
        if exclude_email is not None
        else str((lead_row or {}).get("b2b_contact_email") or "")
    ).strip()

    buyers = get_matching_buyers(
        category=category,
        postcode_area=postcode_area,
        exclude_company_number=ex_num,
        exclude_email=ex_email,
        limit=5,
        db_path=db_path,
    )
    dispatched_emails: list[str] = []
    base_url = settings.PUBLIC_BASE_URL.rstrip("/")

    for buyer in buyers:
        if ex_num and str(buyer.get("company_number") or "").strip().upper() == ex_num.upper():
            continue
        if ex_email and str(buyer.get("b2b_email") or "").strip().lower() == ex_email.lower():
            continue

        unsubscribe_url = f"{base_url}/unsubscribe/{buyer['unsubscribe_token']}"
        subject = f"[{settings.TRADING_NAME}] New {category} Statutory Condition Requirement in {postcode_outward}"
        footer_html = build_pecr_sole_trader_footer(
            company_name=buyer["company_name"],
            company_number=buyer["company_number"],
            unsubscribe_url=unsubscribe_url,
        )
        html_body = (
            f"<p>Dear {buyer['company_name']} Team,</p>"
            f"<p>A UK Council Decision Notice in <strong>{postcode_outward}</strong> has just been issued "
            f"with a mandatory <strong>{category}</strong> condition attached to an active UK corporate applicant:</p>"
            f"<blockquote><em>&ldquo;{verbatim_quote}&rdquo;</em></blockquote>"
            f"<p>We have attached the redacted Teaser Brief (PDF) to this email.</p>"
            f"<p>To unlock the full unredacted Project Dossier (exact site address, planning reference, "
            f"Companies House registration, verified corporate email, and outreach template) for "
            f"<strong>&pound;{settings.LEAD_PRICE_GBP}</strong> (strictly limited to 1 buyer):</p>"
            f"<p><a href='{stripe_payment_url}'><strong>Click here to acquire exclusive Dossier via Stripe (&pound;{settings.LEAD_PRICE_GBP})</strong></a></p>"
            f"{footer_html}"
        )

        msg_id = send_resend_email(
            to_email=buyer["b2b_email"],
            subject=subject,
            html_body=html_body,
            attachment_path=teaser_pdf_path,
            unsubscribe_url=unsubscribe_url,
        )
        record_dispatch(
            lead_id=lead_id,
            buyer_id=int(buyer["id"]),
            resend_message_id=msg_id,
            db_path=db_path,
        )
        dispatched_emails.append(buyer["b2b_email"])

    update_lead(
        app_ref=app_ref,
        updates={"status": LeadStatus.DISPATCHED.value},
        db_path=db_path,
    )
    return dispatched_emails


def merge_into_primary_lead(
    primary_lead: dict[str, Any],
    duplicate_app_ref: str,
    duplicate_quote: str,
    dossiers_dir: Optional[Path] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Merge duplicate_app_ref into primary_lead, regenerate primary Teaser & Full Dossier PDFs, and mark duplicate MERGED_DUPLICATE."""
    init_db(db_path)
    primary_id = int(primary_lead["id"])
    existing_ref = str(primary_lead["app_ref"]).strip()
    base_primary_ref = existing_ref.split(" & ")[0].strip()

    ref_parts = [p.strip() for p in existing_ref.split("&") if p.strip()]
    if duplicate_app_ref not in ref_parts:
        ref_parts.append(duplicate_app_ref)
    merged_app_ref = " & ".join(ref_parts)

    primary_quote = str(primary_lead.get("verbatim_quote") or "").strip()
    dup_quote_clean = (duplicate_quote or "").strip()
    if dup_quote_clean and dup_quote_clean not in primary_quote:
        merged_quote = f"[{base_primary_ref}] {primary_quote}\n\n[{duplicate_app_ref}] {dup_quote_clean}"
    else:
        merged_quote = primary_quote

    # Check if duplicate lead has an active Stripe link to deactivate
    dup_row = get_lead_by_app_ref(duplicate_app_ref, db_path=db_path)
    if dup_row and dup_row.get("id") != primary_id:
        dup_plink = str(dup_row.get("stripe_payment_link_id") or "").strip()
        if dup_plink:
            deactivate_stripe_link(dup_plink)
        update_lead(
            app_ref=duplicate_app_ref,
            updates={"status": LeadStatus.MERGED_DUPLICATE.value},
            db_path=db_path,
        )

    cat_enum = SpecialistCategory(str(primary_lead["category"]))
    dec_enum = DecisionType(str(primary_lead.get("decision_type") or "CONDITIONAL_APPROVAL"))

    merged_parsed = ParsedDecisionNotice(
        application_ref=merged_app_ref,
        site_address=str(primary_lead.get("site_address") or ""),
        postcode=str(primary_lead.get("postcode") or ""),
        decision_type=dec_enum,
        applicant_name=str(primary_lead.get("applicant_name") or ""),
        agent_company_name=str(primary_lead.get("applicant_name") or ""),
        agent_email_on_form=str(primary_lead.get("b2b_contact_email") or ""),
        specialist_triggers=[
            SpecialistTrigger(
                category=cat_enum,
                condition_number="Condition 1 (Combined Statutory Triggers)",
                verbatim_quote=merged_quote,
                pre_commencement=True,
            )
        ],
    )
    merged_verification = CorporateVerificationResult(
        app_ref=merged_app_ref,
        is_commercial=True,
        verified_company_name=str(primary_lead.get("applicant_name") or ""),
        company_number=str(primary_lead.get("company_number") or ""),
        company_type="ltd",
        company_status="active",
        b2b_contact_email=str(primary_lead.get("b2b_contact_email") or ""),
    )

    synthesized = run_agent4_synthesizer(
        parsed=merged_parsed,
        verification=merged_verification,
        output_dir=dossiers_dir,
        db_path=db_path,
    )

    from app.database import db_session
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE leads
            SET app_ref = ?,
                verbatim_quote = ?,
                teaser_pdf_path = ?,
                dossier_pdf_path = ?
            WHERE id = ?
            """,
            (
                merged_app_ref,
                merged_quote,
                synthesized.teaser_pdf_path,
                synthesized.dossier_pdf_path,
                primary_id,
            ),
        )
        updated_row = conn.execute("SELECT * FROM leads WHERE id = ?", (primary_id,)).fetchone()
        return dict(updated_row) if updated_row else primary_lead


def run_agent6_closer(
    parsed: ParsedDecisionNotice,
    dossier: SynthesizedDossier,
    auto_approve: Optional[bool] = None,
    dossiers_dir: Optional[Path] = None,
    db_path: Optional[str] = None,
) -> CloserDispatchResult:
    """Deduplicate by (normalized site_address + category + company_number), create single-use Stripe Payment Link, and dispatch or send Telegram approval card."""
    init_db(db_path)
    lead_row = get_lead_by_app_ref(parsed.application_ref, db_path=db_path)
    lead_id = int(lead_row["id"]) if lead_row else 1
    company_number = str((lead_row or {}).get("company_number") or "")
    b2b_contact_email = str((lead_row or {}).get("b2b_contact_email") or "")
    verbatim_quote = parsed.specialist_triggers[0].verbatim_quote

    # Guardrail 1: Site + Category + Company Deduplication
    duplicate_primary = find_duplicate_primary_lead(
        app_ref=parsed.application_ref,
        site_address=parsed.site_address,
        category=dossier.category.value,
        company_number=company_number,
        db_path=db_path,
    )
    if duplicate_primary:
        logger.info(
            "Merging duplicate lead %s into primary lead %s (%s, %s).",
            parsed.application_ref,
            duplicate_primary["app_ref"],
            parsed.site_address,
            dossier.category.value,
        )
        merged_primary = merge_into_primary_lead(
            primary_lead=duplicate_primary,
            duplicate_app_ref=parsed.application_ref,
            duplicate_quote=verbatim_quote,
            dossiers_dir=dossiers_dir,
            db_path=db_path,
        )
        return CloserDispatchResult(
            app_ref=parsed.application_ref,
            lead_id=lead_id,
            stripe_payment_link_id=str(merged_primary.get("stripe_payment_link_id") or ""),
            stripe_payment_url=str(merged_primary.get("stripe_payment_url") or ""),
            status=LeadStatus.MERGED_DUPLICATE,
            dispatched_buyer_emails=[],
            pending_telegram_approval=False,
        )

    plink_id, plink_url = create_single_use_stripe_link(
        lead_id=lead_id,
        app_ref=parsed.application_ref,
        category=dossier.category.value,
        postcode_outward=dossier.postcode_outward,
    )

    update_lead(
        app_ref=parsed.application_ref,
        updates={
            "stripe_payment_link_id": plink_id,
            "stripe_payment_url": plink_url,
        },
        db_path=db_path,
    )

    is_auto = settings.AUTO_APPROVE_MODE if auto_approve is None else auto_approve
    postcode_area = extract_postcode_area_prefix(parsed.postcode)

    if is_auto:
        sent_to = dispatch_teaser_to_buyers(
            lead_id=lead_id,
            app_ref=parsed.application_ref,
            category=dossier.category.value,
            postcode_area=postcode_area,
            postcode_outward=dossier.postcode_outward,
            verbatim_quote=verbatim_quote,
            teaser_pdf_path=dossier.teaser_pdf_path,
            stripe_payment_url=plink_url,
            exclude_company_number=company_number,
            exclude_email=b2b_contact_email,
            db_path=db_path,
        )
        return CloserDispatchResult(
            app_ref=parsed.application_ref,
            lead_id=lead_id,
            stripe_payment_link_id=plink_id,
            stripe_payment_url=plink_url,
            status=LeadStatus.DISPATCHED,
            dispatched_buyer_emails=sent_to,
            pending_telegram_approval=False,
        )

    # Manual approval mode via Telegram inline buttons
    update_lead(
        app_ref=parsed.application_ref,
        updates={"status": LeadStatus.PENDING_APPROVAL.value},
        db_path=db_path,
    )
    updated_lead_row = get_lead_by_app_ref(parsed.application_ref, db_path=db_path) or (lead_row or {"id": lead_id, "app_ref": parsed.application_ref})
    send_telegram_approval_card(updated_lead_row, db_path=db_path)

    return CloserDispatchResult(
        app_ref=parsed.application_ref,
        lead_id=lead_id,
        stripe_payment_link_id=plink_id,
        stripe_payment_url=plink_url,
        status=LeadStatus.PENDING_APPROVAL,
        dispatched_buyer_emails=[],
        pending_telegram_approval=True,
    )
