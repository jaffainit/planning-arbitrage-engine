"""Phase 4: Sequential Orchestrator connecting Agents 1 -> 2 -> 3 -> 4 -> 5 -> 6."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from app.agents.agent1_harvester import register_local_pdf, run_agent1_harvester
from app.agents.agent2_parser import run_agent2_parser
from app.agents.agent3_verifier import run_agent3_verifier
from app.agents.agent4_synthesizer import run_agent4_synthesizer
from app.agents.agent5_critic import run_agent5_critic
from app.agents.agent6_closer import merge_into_primary_lead, run_agent6_closer
from app.database import db_session, find_duplicate_primary_lead, get_lead_by_app_ref, init_db
from app.models import DecisionType, LeadStatus
from app.seed_buyers import seed_buyers

logger = logging.getLogger(__name__)


def deduplicate_existing_leads(
    dossiers_dir: Optional[Path] = None,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Scan active leads in engine.db and merge any duplicates sharing normalized site_address, category, and company_number."""
    init_db(db_path)
    merged_records: list[dict[str, Any]] = []
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM leads
            WHERE status NOT IN ('IGNORED', 'REJECTED_NON_COMMERCIAL', 'CRITIC_REJECTED', 'MERGED_DUPLICATE')
            ORDER BY id ASC
            """
        ).fetchall()
        active_leads = [dict(r) for r in rows]

    for lead in active_leads:
        current = get_lead_by_app_ref(str(lead["app_ref"]), db_path=db_path)
        if not current or current.get("status") == LeadStatus.MERGED_DUPLICATE.value:
            continue
        primary = find_duplicate_primary_lead(
            app_ref=str(current["app_ref"]),
            site_address=str(current.get("site_address") or ""),
            category=str(current.get("category") or ""),
            company_number=str(current.get("company_number") or ""),
            db_path=db_path,
        )
        if primary and int(primary["id"]) < int(current["id"]):
            updated_primary = merge_into_primary_lead(
                primary_lead=primary,
                duplicate_app_ref=str(current["app_ref"]),
                duplicate_quote=str(current.get("verbatim_quote") or ""),
                dossiers_dir=dossiers_dir,
                db_path=db_path,
            )
            merged_records.append(updated_primary)
    return merged_records


def process_single_pdf(
    pdf_path: str | Path,
    app_ref: Optional[str] = None,
    auto_approve: Optional[bool] = None,
    db_path: Optional[str] = None,
    dossiers_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Run Agents 2 -> 3 -> 4 -> 5 -> 6 sequentially on a single Decision Notice PDF."""
    init_db(db_path)
    path_obj = Path(pdf_path)
    inferred_ref = app_ref or path_obj.stem.replace("_", "/", 1)

    register_local_pdf(app_ref=inferred_ref, pdf_path=path_obj, db_path=db_path)

    # Agent 2: Parse statutory conditions / refusal reasons
    parsed = run_agent2_parser(pdf_path=path_obj, app_ref=inferred_ref, db_path=db_path)
    if (
        parsed.decision_type == DecisionType.UNCONDITIONAL_OR_IRRELEVANT
        or not parsed.specialist_triggers
    ):
        logger.info("Lead %s marked IGNORED by Agent 2.", parsed.application_ref)
        return get_lead_by_app_ref(parsed.application_ref, db_path=db_path) or {}

    # Agent 3: Verify corporate status & resolve B2B email
    verification = run_agent3_verifier(parsed=parsed, db_path=db_path)
    if not verification.is_commercial:
        logger.info(
            "Lead %s marked REJECTED_NON_COMMERCIAL by Agent 3 (%s).",
            parsed.application_ref,
            verification.rejection_reason,
        )
        return get_lead_by_app_ref(parsed.application_ref, db_path=db_path) or {}

    # Agent 4: Generate Redacted Teaser PDF & Unredacted Full Dossier PDF
    dossier = run_agent4_synthesizer(
        parsed=parsed,
        verification=verification,
        output_dir=dossiers_dir,
        db_path=db_path,
    )

    # Agent 5: Deterministic Verbatim Quote & PECR Legal Guardrail
    critic = run_agent5_critic(
        parsed=parsed,
        verification=verification,
        db_path=db_path,
    )
    if not critic.passed:
        logger.info(
            "Lead %s marked CRITIC_REJECTED by Agent 5 (%s).",
            parsed.application_ref,
            critic.rejection_reason,
        )
        return get_lead_by_app_ref(parsed.application_ref, db_path=db_path) or {}

    # Agent 6: Create Single-Use Stripe Payment Link & Dispatch via Resend / Telegram
    run_agent6_closer(
        parsed=parsed,
        dossier=dossier,
        auto_approve=auto_approve,
        dossiers_dir=dossiers_dir,
        db_path=db_path,
    )
    return get_lead_by_app_ref(parsed.application_ref, db_path=db_path) or {}


async def run_daily_pipeline_async(
    db_path: Optional[str] = None,
    pdfs_dir: Optional[Path] = None,
    dossiers_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Run full pipeline: ensure buyers are seeded, run Agent 1 Harvester, then Agents 2->6 for each PDF."""
    init_db(db_path)
    with db_session(db_path) as conn:
        buyer_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM buyers").fetchone()["cnt"]
    if buyer_cnt == 0:
        seed_buyers(db_path=db_path)

    harvested_pdfs = await run_agent1_harvester(output_dir=pdfs_dir, db_path=db_path)
    processed_leads: list[dict[str, Any]] = []

    for item in harvested_pdfs:
        lead_record = process_single_pdf(
            pdf_path=item.pdf_path,
            app_ref=item.app_ref,
            db_path=db_path,
            dossiers_dir=dossiers_dir,
        )
        if lead_record:
            processed_leads.append(lead_record)

    deduplicate_existing_leads(dossiers_dir=dossiers_dir, db_path=db_path)
    return processed_leads


def run_daily_pipeline(
    db_path: Optional[str] = None,
    pdfs_dir: Optional[Path] = None,
    dossiers_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Synchronous entrypoint for APScheduler and CLI execution."""
    return asyncio.run(
        run_daily_pipeline_async(
            db_path=db_path,
            pdfs_dir=pdfs_dir,
            dossiers_dir=dossiers_dir,
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = run_daily_pipeline()
    print(f"Completed orchestrator run. Processed {len(results)} leads.")

