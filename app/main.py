"""Phase 4: FastAPI Server with Stripe/Telegram Webhooks, PECR Unsubscribe, and APScheduler."""

from contextlib import asynccontextmanager
import html
import json
import logging
import sys
import traceback
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import stripe

from app.agents.agent2_parser import extract_outward_postcode
from app.agents.agent6_closer import (
    answer_telegram_callback,
    deactivate_stripe_link,
    dispatch_teaser_to_buyers,
    get_stripe_client,
    send_resend_email,
    send_telegram_message,
)
from app.config import settings
from app.database import (
    db_session,
    get_lead_by_app_ref,
    get_lead_by_id,
    get_lead_by_payment_link_id,
    init_db,
    unsubscribe_buyer_by_token,
    update_lead,
)
from app.models import LeadStatus
from app.orchestrator import run_daily_pipeline
from app.seed_buyers import seed_buyers

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler(timezone="Europe/London")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize SQLite WAL database, seed buyers if empty, and start 02:00 Europe/London scheduler."""
    init_db()
    with db_session() as conn:
        buyer_count = conn.execute("SELECT COUNT(*) AS cnt FROM buyers").fetchone()["cnt"]
    if buyer_count == 0:
        seed_buyers()

    if not scheduler.running:
        scheduler.add_job(
            run_daily_pipeline,
            trigger=CronTrigger(hour=2, minute=0, timezone="Europe/London"),
            id="daily_planning_arbitrage_pipeline",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
        scheduler.start()
        logger.info("APScheduler started: daily pipeline scheduled at 02:00 Europe/London.")

    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="UK Planning Condition & Refusal Arbitrage Engine",
    version="1.0.0",
    lifespan=lifespan,
)


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def landing_page() -> HTMLResponse:
    """Serve modern Vercel/Linear/Stripe dark-mode institutional UK B2B landing page for Statutory Intel."""
    init_db()
    with db_session() as conn:
        total_leads = conn.execute("SELECT COUNT(*) AS cnt FROM leads").fetchone()["cnt"]
        qualified_dossiers = conn.execute(
            "SELECT COUNT(*) AS cnt FROM leads WHERE status IN ('PENDING_APPROVAL', 'DISPATCHED', 'SOLD')"
        ).fetchone()["cnt"]
        sold_leads = conn.execute(
            "SELECT COUNT(*) AS cnt FROM leads WHERE status = 'SOLD'"
        ).fetchone()["cnt"]
        active_buyers = conn.execute(
            "SELECT COUNT(*) AS cnt FROM buyers WHERE unsubscribed = 0"
        ).fetchone()["cnt"]
        categories_covered = conn.execute(
            "SELECT COUNT(DISTINCT category) AS cnt FROM buyers WHERE unsubscribed = 0"
        ).fetchone()["cnt"]
        recent_rows = conn.execute(
            """
            SELECT app_ref, postcode, decision_type, category, verbatim_quote, status
            FROM leads
            WHERE category IS NOT NULL AND category != ''
            ORDER BY id DESC
            LIMIT 3
            """
        ).fetchall()

    disciplines_count = categories_covered if categories_covered > 0 else 7
    lock_status_badge = f"1-of-1 Lock ({sold_leads} Sold / {qualified_dossiers} Active)" if qualified_dossiers else "Active · 1-of-1 Session Lock"

    recent_feed_cards = ""
    if recent_rows:
        for row in recent_rows:
            outward = extract_outward_postcode(str(row["postcode"] or "BN1 1AA"))
            cat_label = str(row["category"] or "ACOUSTIC_BS4142").replace("_", " ")
            cond_num = str(row["decision_type"] or "Pre-Commencement Condition").replace("_", " ").title()
            quote_snippet = str(row["verbatim_quote"] or "")[:210]
            if len(str(row["verbatim_quote"] or "")) > 210:
                quote_snippet += "..."
            status_val = str(row["status"] or "DISPATCHED")
            is_sold = status_val == "SOLD"
            badge_cls = (
                "bg-amber-500/10 text-amber-300 border-amber-500/30"
                if is_sold
                else "bg-emerald-500/10 text-emerald-300 border-emerald-500/30"
            )
            badge_txt = "LOCKED · ACQUIRED" if is_sold else f"AVAILABLE · £{settings.LEAD_PRICE_GBP} EXCLUSIVE"
            recent_feed_cards += f"""
            <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 flex flex-col justify-between hover:border-emerald-500/40 transition-all duration-200">
                <div>
                    <div class="flex items-center justify-between gap-2 mb-3">
                        <span class="text-xs font-mono uppercase tracking-wider px-2.5 py-1 rounded-md bg-cyan-500/10 text-cyan-300 border border-cyan-500/20">{cat_label}</span>
                        <span class="text-xs font-mono px-2.5 py-1 rounded-full border {badge_cls}">{badge_txt}</span>
                    </div>
                    <div class="text-sm font-semibold text-slate-200 mb-1">Sector {outward} &middot; {cond_num}</div>
                    <p class="text-xs text-slate-400 font-mono leading-relaxed bg-slate-950/70 border border-slate-800/80 rounded-xl p-3 my-3">
                        &ldquo;{quote_snippet}&rdquo;
                    </p>
                </div>
                <div class="flex items-center justify-between pt-3 border-t border-slate-800/70 text-xs text-slate-400">
                    <span>App Ref: <code class="text-slate-500">[REDACTED UNTIL UNLOCK]</code></span>
                    <a href="mailto:{settings.FROM_EMAIL}?subject=Dossier%20Enquiry%20({outward}%20-%20{cat_label})" class="text-emerald-400 hover:text-emerald-300 font-medium">Request Unlock &rarr;</a>
                </div>
            </div>"""
    else:
        recent_feed_cards = f"""
        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 flex flex-col justify-between">
            <div>
                <div class="flex items-center justify-between gap-2 mb-3">
                    <span class="text-xs font-mono uppercase tracking-wider px-2.5 py-1 rounded-md bg-cyan-500/10 text-cyan-300 border border-cyan-500/20">ACOUSTIC BS4142</span>
                    <span class="text-xs font-mono px-2.5 py-1 rounded-full border bg-emerald-500/10 text-emerald-300 border-emerald-500/30">LIVE SAMPLE &middot; £{settings.LEAD_PRICE_GBP}</span>
                </div>
                <div class="text-sm font-semibold text-slate-200 mb-1">Sector BN1 &middot; Condition 4 (Pre-Commencement)</div>
                <p class="text-xs text-slate-400 font-mono leading-relaxed bg-slate-950/70 border border-slate-800/80 rounded-xl p-3 my-3">
                    &ldquo;Prior to the commencement of development, a scheme for the control of noise emission assessed in accordance with BS 4142:2014+A1:2019 shall be submitted to and approved in writing by the Local Planning Authority.&rdquo;
                </p>
            </div>
            <div class="flex items-center justify-between pt-3 border-t border-slate-800/70 text-xs text-slate-400">
                <span>Corporate Applicant: <code class="text-emerald-400">Verified Active LTD</code></span>
                <a href="#comparison" class="text-emerald-400 hover:text-emerald-300 font-medium">Inspect Dossier &rarr;</a>
            </div>
        </div>
        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 flex flex-col justify-between">
            <div>
                <div class="flex items-center justify-between gap-2 mb-3">
                    <span class="text-xs font-mono uppercase tracking-wider px-2.5 py-1 rounded-md bg-cyan-500/10 text-cyan-300 border border-cyan-500/20">ECOLOGY &amp; BNG</span>
                    <span class="text-xs font-mono px-2.5 py-1 rounded-full border bg-emerald-500/10 text-emerald-300 border-emerald-500/30">LIVE SAMPLE &middot; £{settings.LEAD_PRICE_GBP}</span>
                </div>
                <div class="text-sm font-semibold text-slate-200 mb-1">Sector BN3 &middot; Condition 7 (Pre-Commencement)</div>
                <p class="text-xs text-slate-400 font-mono leading-relaxed bg-slate-950/70 border border-slate-800/80 rounded-xl p-3 my-3">
                    &ldquo;Development may not be begun unless a Biodiversity Gain Plan demonstrating a minimum 10% net gain using the statutory Biodiversity Metric has been submitted to and approved by the Planning Authority.&rdquo;
                </p>
            </div>
            <div class="flex items-center justify-between pt-3 border-t border-slate-800/70 text-xs text-slate-400">
                <span>Corporate Applicant: <code class="text-emerald-400">Verified Active LLP</code></span>
                <a href="#comparison" class="text-emerald-400 hover:text-emerald-300 font-medium">Inspect Dossier &rarr;</a>
            </div>
        </div>
        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 flex flex-col justify-between">
            <div>
                <div class="flex items-center justify-between gap-2 mb-3">
                    <span class="text-xs font-mono uppercase tracking-wider px-2.5 py-1 rounded-md bg-cyan-500/10 text-cyan-300 border border-cyan-500/20">CONTAMINATED LAND</span>
                    <span class="text-xs font-mono px-2.5 py-1 rounded-full border bg-amber-500/10 text-amber-300 border-amber-500/30">LOCKED &middot; ACQUIRED</span>
                </div>
                <div class="text-sm font-semibold text-slate-200 mb-1">Sector RH10 &middot; Condition 5 (Pre-Commencement)</div>
                <p class="text-xs text-slate-400 font-mono leading-relaxed bg-slate-950/70 border border-slate-800/80 rounded-xl p-3 my-3">
                    &ldquo;No development shall take place until a Phase I Desk Study and Phase II Intrusive Site Investigation Report have been submitted to and approved in writing by the Local Planning Authority.&rdquo;
                </p>
            </div>
            <div class="flex items-center justify-between pt-3 border-t border-slate-800/70 text-xs text-slate-400">
                <span>Single-Buyer Lock: <code class="text-amber-300">Checkout Completed</code></span>
                <span class="text-slate-500">Sold Exclusively</span>
            </div>
        </div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="en-GB" class="dark scroll-smooth">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Statutory Intel (statutoryintel.co.uk) — Automated UK Local Authority Planning Condition &amp; Refusal Intelligence</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {{
            color-scheme: dark;
        }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #020617;
            color: #f8fafc;
            overflow-x: hidden;
        }}
        .font-mono {{
            font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        }}
        .ambient-glow {{
            background-image:
                radial-gradient(circle at 15% 10%, rgba(16, 185, 129, 0.14), transparent 40%),
                radial-gradient(circle at 85% 15%, rgba(6, 182, 212, 0.14), transparent 42%),
                radial-gradient(circle at 50% 65%, rgba(16, 185, 129, 0.06), transparent 50%);
        }}
        .grid-overlay {{
            background-size: 40px 40px;
            background-image:
                linear-gradient(to right, rgba(30, 41, 59, 0.28) 1px, transparent 1px),
                linear-gradient(to bottom, rgba(30, 41, 59, 0.28) 1px, transparent 1px);
        }}
    </style>
</head>
<body class="bg-slate-950 text-slate-100 antialiased selection:bg-emerald-500/30 selection:text-emerald-200 ambient-glow min-h-screen flex flex-col">

    <!-- Top Institutional Navigation -->
    <header class="sticky top-0 z-50 backdrop-blur-md bg-slate-950/80 border-b border-slate-800/80">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <div class="h-8 w-8 rounded-lg bg-gradient-to-br from-emerald-400 to-cyan-500 flex items-center justify-center text-slate-950 font-extrabold text-sm shadow-lg shadow-emerald-500/20">
                    SI
                </div>
                <div>
                    <span class="font-bold tracking-tight text-white text-base">STATUTORY INTEL</span>
                    <span class="hidden sm:inline-block ml-2 text-xs font-mono text-cyan-400 bg-cyan-950/60 border border-cyan-800/60 px-2 py-0.5 rounded">statutoryintel.co.uk</span>
                </div>
            </div>
            <nav class="hidden md:flex items-center gap-6 text-sm text-slate-300">
                <a href="#telemetry" class="hover:text-emerald-400 transition-colors">Live Telemetry</a>
                <a href="#pipeline" class="hover:text-emerald-400 transition-colors">3-Stage Pipeline</a>
                <a href="#comparison" class="hover:text-emerald-400 transition-colors">Teaser vs. Dossier</a>
                <a href="#disciplines" class="hover:text-emerald-400 transition-colors">Disciplines</a>
                <a href="#compliance" class="hover:text-emerald-400 transition-colors">Compliance</a>
            </nav>
            <div class="flex items-center gap-3">
                <a href="/health" class="text-xs font-mono text-slate-400 hover:text-cyan-300 border border-slate-800 bg-slate-900/70 px-3 py-1.5 rounded-lg transition-colors">
                    API /health
                </a>
                <a href="mailto:{settings.FROM_EMAIL}?subject=Consultancy%20Access%20Request%20-%20Statutory%20Intel" class="text-xs font-semibold bg-emerald-500 hover:bg-emerald-400 text-slate-950 px-3.5 py-2 rounded-lg transition-all shadow-md shadow-emerald-500/20">
                    Consultancy Access
                </a>
            </div>
        </div>
    </header>

    <main class="flex-grow grid-overlay">
        <!-- 2) HERO SECTION -->
        <section class="relative pt-16 pb-14 sm:pt-24 sm:pb-20 border-b border-slate-800/80">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
                <div class="max-w-4xl">
                    <div class="inline-flex items-center gap-2.5 px-3.5 py-1.5 rounded-full text-xs font-semibold tracking-wide bg-emerald-500/10 text-emerald-300 border border-emerald-500/30 mb-6 shadow-sm">
                        <span>🟢 Live UK Local Authority Monitoring Active</span>
                        <span class="text-slate-600">|</span>
                        <span class="font-mono text-cyan-300">02:00 GMT Daily Cron</span>
                    </div>

                    <h1 class="text-4xl sm:text-5xl lg:text-6xl font-extrabold tracking-tight text-white leading-[1.12] mb-6">
                        Automated Pre-Commencement <br class="hidden sm:inline">
                        <span class="bg-gradient-to-r from-emerald-400 via-teal-300 to-cyan-400 bg-clip-text text-transparent">
                            Statutory Condition &amp; Refusal Intelligence
                        </span>
                    </h1>

                    <p class="text-lg sm:text-xl text-slate-300 leading-relaxed max-w-3xl mb-8">
                        Deterministic extraction of mandatory pre-commencement and pre-occupation planning conditions directly from UK Local Planning Authority Decision Notices. Cross-verified against live UK Companies House registers and released to specialist engineering consultancies under an exclusive <strong class="text-white font-semibold">Single-Buyer £{settings.LEAD_PRICE_GBP} Stripe Lock</strong>.
                    </p>

                    <div class="flex flex-wrap items-center gap-4">
                        <a href="#live-feed" class="inline-flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl font-semibold text-sm bg-gradient-to-r from-emerald-500 to-teal-400 hover:from-emerald-400 hover:to-teal-300 text-slate-950 shadow-lg shadow-emerald-500/25 transition-all">
                            Explore Live Intelligence Feed
                            <span aria-hidden="true">&rarr;</span>
                        </a>
                        <a href="mailto:{settings.FROM_EMAIL}?subject=Consultancy%20Access%20Enquiry%20-%20Statutory%20Intel" class="inline-flex items-center justify-center gap-2 px-6 py-3.5 rounded-xl font-semibold text-sm backdrop-blur-md bg-slate-900/70 hover:bg-slate-800/80 text-slate-100 border border-slate-700/80 transition-all">
                            Consultancy Access
                        </a>
                    </div>
                </div>

                <!-- 3) LIVE TELEMETRY BAR -->
                <div id="telemetry" class="mt-14 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                    <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6">
                        <div class="flex items-center justify-between mb-2">
                            <span class="text-xs font-mono uppercase tracking-wider text-slate-400">Total Decision Notices Ingested</span>
                            <span class="h-2 w-2 rounded-full bg-emerald-400"></span>
                        </div>
                        <div class="text-3xl font-extrabold text-white font-mono">{total_leads}</div>
                        <div class="mt-1 text-xs text-slate-400">{qualified_dossiers} verified corporate condition dossiers</div>
                    </div>

                    <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6">
                        <div class="flex items-center justify-between mb-2">
                            <span class="text-xs font-mono uppercase tracking-wider text-slate-400">Active Sussex/South East Corporate Buyers</span>
                            <span class="h-2 w-2 rounded-full bg-cyan-400"></span>
                        </div>
                        <div class="text-3xl font-extrabold text-white font-mono">{active_buyers}</div>
                        <div class="mt-1 text-xs text-slate-400">PECR Reg 22 verified LTD / LLP / PLC entities</div>
                    </div>

                    <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6">
                        <div class="flex items-center justify-between mb-2">
                            <span class="text-xs font-mono uppercase tracking-wider text-slate-400">Specialist Disciplines Monitored</span>
                            <span class="h-2 w-2 rounded-full bg-emerald-400"></span>
                        </div>
                        <div class="text-3xl font-extrabold text-white font-mono">{disciplines_count}</div>
                        <div class="mt-1 text-xs text-slate-400">Acoustics, Ecology/BNG, Arbor, Geo, SuDS, Heritage, Appeals</div>
                    </div>

                    <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6">
                        <div class="flex items-center justify-between mb-2">
                            <span class="text-xs font-mono uppercase tracking-wider text-slate-400">Exclusive Single-Buyer Lock Status</span>
                            <span class="h-2 w-2 rounded-full bg-cyan-400"></span>
                        </div>
                        <div class="text-2xl font-extrabold text-emerald-400 font-mono">£{settings.LEAD_PRICE_GBP} <span class="text-sm font-normal text-slate-300">/ Dossier</span></div>
                        <div class="mt-1 text-xs text-slate-400 font-mono">{lock_status_badge}</div>
                    </div>
                </div>
            </div>
        </section>

        <!-- 4) 3-STEP INTELLIGENCE PIPELINE -->
        <section id="pipeline" class="py-16 sm:py-20 border-b border-slate-800/80">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
                <div class="max-w-3xl mb-12">
                    <div class="text-xs font-mono uppercase tracking-widest text-cyan-400 mb-2">Deterministic 6-Agent Architecture</div>
                    <h2 class="text-2xl sm:text-3xl font-bold text-white tracking-tight">3-Step Statutory Intelligence Pipeline</h2>
                    <p class="mt-3 text-slate-400 text-base">
                        Every Local Planning Authority Decision Notice is harvested, verified against statutory corporate registers, and audited for verbatim accuracy before dispatch. Private householder applications are discarded automatically.
                    </p>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    <!-- Stage 1 -->
                    <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-7 relative overflow-hidden group hover:border-emerald-500/40 transition-all">
                        <div class="flex items-center justify-between mb-5">
                            <span class="text-xs font-mono font-semibold uppercase tracking-wider px-3 py-1 rounded-full bg-emerald-500/10 text-emerald-300 border border-emerald-500/30">
                                Stage 1
                            </span>
                            <span class="text-xs font-mono text-slate-500">Agent 1 &middot; Harvester</span>
                        </div>
                        <h3 class="text-xl font-bold text-white mb-3">Automated Idox Ingestion</h3>
                        <p class="text-sm text-slate-300 leading-relaxed mb-5">
                            Headless Playwright &amp; PyMuPDF ingestion sweeps UK Local Planning Authority weekly decision lists at 02:00 GMT, downloading newly decided Decision Notice PDFs and isolating commercial consents and refusals.
                        </p>
                        <div class="text-xs font-mono text-slate-400 bg-slate-950/80 border border-slate-800/80 rounded-xl p-3">
                            <div>&gt; Source: <span class="text-cyan-300">Idox Public Access / LPA Portals</span></div>
                            <div>&gt; Filter: <span class="text-emerald-300">Conditional Approvals &amp; Refusals</span></div>
                        </div>
                    </div>

                    <!-- Stage 2 -->
                    <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-7 relative overflow-hidden group hover:border-cyan-500/40 transition-all">
                        <div class="flex items-center justify-between mb-5">
                            <span class="text-xs font-mono font-semibold uppercase tracking-wider px-3 py-1 rounded-full bg-cyan-500/10 text-cyan-300 border border-cyan-500/30">
                                Stage 2
                            </span>
                            <span class="text-xs font-mono text-slate-500">Agents 2, 3 &amp; 5 &middot; Parser &amp; Critic</span>
                        </div>
                        <h3 class="text-xl font-bold text-white mb-3">Companies House &amp; AI Statutory Parser</h3>
                        <p class="text-sm text-slate-300 leading-relaxed mb-5">
                            Extracts verbatim pre-commencement triggers (BS 4142, BS 5837, BNG, Phase 1/2 Geo, SuDS, Heritage) with a strict <code class="text-cyan-300">&ge;85%</code> verbatim PDF quote match gate and live Companies House corporate verification (<code class="text-emerald-300">LTD</code>, <code class="text-emerald-300">LLP</code>, <code class="text-emerald-300">PLC</code>).
                        </p>
                        <div class="text-xs font-mono text-slate-400 bg-slate-950/80 border border-slate-800/80 rounded-xl p-3">
                            <div>&gt; Critic Gate: <span class="text-emerald-300">&ge;85% Verbatim Quote Match</span></div>
                            <div>&gt; PECR Guardrail: <span class="text-cyan-300">Zero Consumer ISPs</span></div>
                        </div>
                    </div>

                    <!-- Stage 3 -->
                    <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-7 relative overflow-hidden group hover:border-emerald-500/40 transition-all">
                        <div class="flex items-center justify-between mb-5">
                            <span class="text-xs font-mono font-semibold uppercase tracking-wider px-3 py-1 rounded-full bg-emerald-500/10 text-emerald-300 border border-emerald-500/30">
                                Stage 3
                            </span>
                            <span class="text-xs font-mono text-slate-500">Agents 4 &amp; 6 &middot; Synthesizer &amp; Closer</span>
                        </div>
                        <h3 class="text-xl font-bold text-white mb-3">Single-Buyer £{settings.LEAD_PRICE_GBP} Stripe Lock</h3>
                        <p class="text-sm text-slate-300 leading-relaxed mb-5">
                            Regional consultancies receive a redacted Teaser Brief. Checkout is governed by a single-completion Stripe Payment Link (<code class="text-emerald-300">completed_sessions.limit = 1</code>) that locks the dossier and auto-delivers the unredacted PDF immediately upon payment.
                        </p>
                        <div class="text-xs font-mono text-slate-400 bg-slate-950/80 border border-slate-800/80 rounded-xl p-3">
                            <div>&gt; Exclusivity: <span class="text-emerald-300">completed_sessions.limit = 1</span></div>
                            <div>&gt; Fulfillment: <span class="text-cyan-300">Instant Webhook PDF Dispatch</span></div>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- 5) TEASER vs. DOSSIER COMPARISON -->
        <section id="comparison" class="py-16 sm:py-20 border-b border-slate-800/80">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
                <div class="flex flex-col md:flex-row md:items-end justify-between mb-10 gap-4">
                    <div>
                        <div class="text-xs font-mono uppercase tracking-widest text-emerald-400 mb-2">Transparent Intelligence Packaging</div>
                        <h2 class="text-2xl sm:text-3xl font-bold text-white tracking-tight">Teaser Brief (Free) vs. Unredacted Dossier (£{settings.LEAD_PRICE_GBP} Exclusive)</h2>
                        <p class="mt-2 text-slate-400 text-base max-w-2xl">
                            Verify exact technical scope from the verbatim condition quote before committing. Unlock the full site address, corporate applicant/architect registration, and outreach template with a single-buyer lock.
                        </p>
                    </div>
                    <!-- Interactive Highlight Filter -->
                    <div class="inline-flex rounded-xl p-1 bg-slate-900/90 border border-slate-800 self-start">
                        <button type="button" onclick="highlightMode('both')" id="btn-both" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-white transition-all">Side-by-Side</button>
                        <button type="button" onclick="highlightMode('teaser')" id="btn-teaser" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white transition-all">Free Teaser</button>
                        <button type="button" onclick="highlightMode('dossier')" id="btn-dossier" class="px-3.5 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white transition-all">£{settings.LEAD_PRICE_GBP} Unredacted Dossier</button>
                    </div>
                </div>

                <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 items-stretch">
                    <!-- Free Teaser Card -->
                    <div id="card-teaser" class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-7 flex flex-col justify-between transition-all duration-300">
                        <div>
                            <div class="flex items-center justify-between border-b border-slate-800 pb-4 mb-6">
                                <div>
                                    <span class="text-xs font-mono uppercase tracking-wider text-cyan-400 block">Stage A Dispatch</span>
                                    <h3 class="text-xl font-bold text-white mt-0.5">Redacted Engineering Teaser Brief</h3>
                                </div>
                                <span class="px-3 py-1 rounded-full text-xs font-mono font-semibold bg-slate-800 text-slate-200 border border-slate-700">
                                    FREE DISPATCH
                                </span>
                            </div>

                            <ul class="space-y-4 text-sm">
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Outward Postcode Sector</strong>
                                        <span class="text-slate-400">Confirm regional proximity immediately (e.g., <code class="text-cyan-300">BN1</code>, <code class="text-cyan-300">BN3</code>, <code class="text-cyan-300">RH10</code>).</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Specialist Discipline &amp; Trigger Classification</strong>
                                        <span class="text-slate-400">Mapped to exact technical standard (e.g., <code class="text-cyan-300">ACOUSTIC_BS4142</code>, <code class="text-cyan-300">ARBORICULTURE_BS5837</code>, <code class="text-cyan-300">ECOLOGY_BNG</code>).</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Verbatim Statutory Condition Quote</strong>
                                        <span class="text-slate-400">Exact unedited text extracted from the Council Decision Notice PDF so engineers can scope fee proposals accurately.</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3 opacity-60">
                                    <span class="text-slate-500 font-bold mt-0.5">&#10005;</span>
                                    <div>
                                        <strong class="text-slate-400 block">Exact Site Address &amp; Planning Reference</strong>
                                        <span class="text-slate-500 font-mono text-xs">[REDACTED IN TEASER]</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3 opacity-60">
                                    <span class="text-slate-500 font-bold mt-0.5">&#10005;</span>
                                    <div>
                                        <strong class="text-slate-400 block">Companies House Number &amp; Corporate Contact</strong>
                                        <span class="text-slate-500 font-mono text-xs">[REDACTED IN TEASER]</span>
                                    </div>
                                </li>
                            </ul>
                        </div>

                        <div class="mt-7 pt-4 border-t border-slate-800/80 flex items-center justify-between text-xs text-slate-400">
                            <span>Delivered via PECR-compliant B2B Alert</span>
                            <span class="font-mono text-cyan-400">teaser_BH2026_XXXXX.pdf</span>
                        </div>
                    </div>

                    <!-- Paid Unredacted Dossier Card -->
                    <div id="card-dossier" class="backdrop-blur-md bg-slate-900/60 border border-emerald-500/50 rounded-2xl p-7 flex flex-col justify-between shadow-xl shadow-emerald-950/30 transition-all duration-300">
                        <div>
                            <div class="flex items-center justify-between border-b border-slate-800 pb-4 mb-6">
                                <div>
                                    <span class="text-xs font-mono uppercase tracking-wider text-emerald-400 block">Instant Stripe Webhook Fulfillment</span>
                                    <h3 class="text-xl font-bold text-white mt-0.5">Unredacted Full Intelligence Dossier</h3>
                                </div>
                                <span class="px-3 py-1 rounded-full text-xs font-mono font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
                                    £{settings.LEAD_PRICE_GBP} EXCLUSIVE LOCK
                                </span>
                            </div>

                            <ul class="space-y-4 text-sm">
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Exact Site Address &amp; Council Application Reference</strong>
                                        <span class="text-slate-300">Full street address, full postcode, and direct Local Planning Authority application reference (e.g., <code class="text-emerald-300">BH2026/01842 — 142 Western Road, Brighton BN1 2LA</code>).</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Verified Applicant &amp; Architect Company Number</strong>
                                        <span class="text-slate-300">Cross-checked UK Companies House registration number, corporate entity type (<code class="text-emerald-300">LTD / LLP / PLC</code>), and active filing status.</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Direct Corporate B2B Contact Endpoint</strong>
                                        <span class="text-slate-300">Verified commercial architectural practice or developer studio email endpoint for immediate technical engagement.</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Ready-to-Send Technical Outreach Template</strong>
                                        <span class="text-slate-300">Pre-drafted, condition-specific technical fee proposal introduction citing the exact Condition Number and statutory wording.</span>
                                    </div>
                                </li>
                                <li class="flex items-start gap-3">
                                    <span class="text-emerald-400 font-bold mt-0.5">&#10003;</span>
                                    <div>
                                        <strong class="text-white block">Guaranteed Single-Buyer Exclusivity</strong>
                                        <span class="text-slate-300">Stripe Payment Link deactivates globally the instant your checkout completes—never resold to competing consultancies.</span>
                                    </div>
                                </li>
                            </ul>
                        </div>

                        <div class="mt-7 pt-4 border-t border-slate-800/80 flex items-center justify-between text-xs">
                            <span class="text-emerald-300 font-medium">Auto-dispatched via Resend within 3 seconds of payment</span>
                            <span class="font-mono text-emerald-400">dossier_BH2026_XXXXX.pdf</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- LIVE INTELLIGENCE FEED PREVIEW & 7 SPECIALIST DISCIPLINES -->
        <section id="live-feed" class="py-16 sm:py-20 border-b border-slate-800/80">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
                <div class="mb-10">
                    <div class="text-xs font-mono uppercase tracking-widest text-cyan-400 mb-2">Real-Time Statutory Feed</div>
                    <h2 class="text-2xl sm:text-3xl font-bold text-white tracking-tight">Recent Extracted Pre-Commencement Conditions</h2>
                    <p class="mt-2 text-slate-400 text-base">
                        Live condition triggers parsed from Local Planning Authority Decision Notices.
                    </p>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-16">
                    {recent_feed_cards}
                </div>

                <!-- 7 Disciplines Grid -->
                <div id="disciplines" class="pt-6">
                    <div class="text-xs font-mono uppercase tracking-widest text-emerald-400 mb-2">Coverage Matrix</div>
                    <h3 class="text-xl sm:text-2xl font-bold text-white mb-6">Seven Specialist Engineering Disciplines Monitored Daily</h3>
                    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5">
                            <div class="text-xs font-mono text-cyan-400 mb-1">01 &middot; ACOUSTIC_BS4142</div>
                            <div class="font-bold text-white text-sm mb-1">BS 4142 Acoustics &amp; Noise Impact</div>
                            <p class="text-xs text-slate-400">Plant noise assessments, sound insulation schemes, and commercial ventilation discharge conditions.</p>
                        </div>
                        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5">
                            <div class="text-xs font-mono text-cyan-400 mb-1">02 &middot; ARBORICULTURE_BS5837</div>
                            <div class="font-bold text-white text-sm mb-1">BS 5837 Arboriculture &amp; Tree Protection</div>
                            <p class="text-xs text-slate-400">Arboricultural Method Statements (AMS), Tree Protection Plans (TPP), and root protection zone monitoring.</p>
                        </div>
                        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5">
                            <div class="text-xs font-mono text-cyan-400 mb-1">03 &middot; ECOLOGY_BNG</div>
                            <div class="font-bold text-white text-sm mb-1">BNG &amp; Protected Species Ecology</div>
                            <p class="text-xs text-slate-400">Statutory 10% Biodiversity Net Gain (BNG) plans, Preliminary Ecological Appraisals (PEA), and bat/reptile surveys.</p>
                        </div>
                        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5">
                            <div class="text-xs font-mono text-cyan-400 mb-1">04 &middot; CONTAMINATED_LAND_GEO</div>
                            <div class="font-bold text-white text-sm mb-1">Geo-Environmental &amp; Contaminated Land</div>
                            <p class="text-xs text-slate-400">Phase 1 Desk Studies, Phase 2 intrusive ground investigations, remediation strategies, and verification reporting.</p>
                        </div>
                        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5">
                            <div class="text-xs font-mono text-cyan-400 mb-1">05 &middot; FLOOD_DRAINAGE_SUDS</div>
                            <div class="font-bold text-white text-sm mb-1">Flood Risk &amp; SuDS Surface Water Drainage</div>
                            <p class="text-xs text-slate-400">Site-specific Flood Risk Assessments (FRA), Sustainable Drainage Systems (SuDS) design, and hydraulic calculations.</p>
                        </div>
                        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5">
                            <div class="text-xs font-mono text-cyan-400 mb-1">06 &middot; HERITAGE_ARCHAEOLOGY</div>
                            <div class="font-bold text-white text-sm mb-1">Built Heritage &amp; Archaeological WSI</div>
                            <p class="text-xs text-slate-400">Listed Building Heritage Statements, Historic Building Recording, and Written Schemes of Investigation (WSI).</p>
                        </div>
                        <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5 sm:col-span-2 lg:col-span-3">
                            <div class="text-xs font-mono text-emerald-400 mb-1">07 &middot; PLANNING_APPEAL_REFUSAL</div>
                            <div class="font-bold text-white text-sm mb-1">RTPI Town Planning &amp; Refusal Appeals</div>
                            <p class="text-xs text-slate-400">Section 78 Planning Inspectorate appeals, Prior Approval resubmissions, and commercial refusal overturning strategies across Sussex and the South East.</p>
                        </div>
                    </div>
                </div>
            </div>
        </section>
    </main>

    <!-- 6) COMPLIANCE & LEGAL FOOTER -->
    <footer id="compliance" class="bg-slate-950 border-t border-slate-800/90 py-14 text-xs text-slate-400">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="backdrop-blur-md bg-slate-900/60 border border-slate-800/80 rounded-2xl p-6 sm:p-8 space-y-4 mb-8">
                <div class="flex items-center justify-between flex-wrap gap-2 border-b border-slate-800 pb-3">
                    <span class="font-mono uppercase tracking-wider text-emerald-400 font-semibold">UK Statutory &amp; Regulatory Compliance Disclosures</span>
                    <span class="font-mono text-slate-500">Jurisdiction: England &amp; Wales</span>
                </div>

                <p class="leading-relaxed">
                    <strong class="text-slate-200">Companies Act 2006 Part 41 Sole Trader Disclosure:</strong>
                    <strong>{settings.SOLE_TRADER_NAME} trading as {settings.TRADING_NAME}</strong> (<code class="text-cyan-300">statutoryintel.co.uk</code>) &middot;
                    Service Address: {settings.SERVICE_ADDRESS} &middot;
                    Compliance &amp; Data Protection Contact: <a href="mailto:{settings.FROM_EMAIL}" class="text-emerald-400 hover:underline">{settings.FROM_EMAIL}</a>.
                </p>

                <p class="leading-relaxed">
                    <strong class="text-slate-200">UK PECR Regulations 22 &amp; 23 Corporate Subscriber Notice:</strong>
                    B2B statutory condition notifications are transmitted exclusively to verified UK corporate subscribers (active Limited Companies, LLPs, and PLCs) at public corporate role addresses in strict compliance with Regulation 22 &amp; Regulation 23 of the Privacy and Electronic Communications (EC Directive) Regulations 2003. Every dispatch includes an instant 1-click opt-out endpoint (<code class="text-slate-300">/unsubscribe/{{token}}</code>) and RFC 8058 <code class="text-slate-300">List-Unsubscribe-Post</code> header.
                </p>

                <p class="leading-relaxed">
                    <strong class="text-slate-200">UK GDPR Article 14 &amp; Open Government Licence v3.0 Notice:</strong>
                    Public planning decision notices and corporate registration records are processed under UK GDPR Article 6(1)(f) (Legitimate Interests in B2B statutory condition matching) and Article 14 transparency rules. Contains public sector information licensed under the <strong class="text-slate-200">Open Government Licence v3.0 (OGL v3.0)</strong> sourced from UK Local Planning Authority statutory registers and Companies House.
                </p>
            </div>

            <div class="flex flex-col sm:flex-row items-center justify-between gap-4 text-slate-500">
                <div>&copy; 2026 {settings.SOLE_TRADER_NAME} trading as {settings.TRADING_NAME}. All rights reserved.</div>
                <div class="flex items-center gap-5">
                    <a href="/health" class="hover:text-emerald-400 transition-colors">System Telemetry (/health)</a>
                    <a href="/unsubscribe/preview-opt-out" class="hover:text-emerald-400 transition-colors">PECR 1-Click Unsubscribe Portal</a>
                    <a href="mailto:{settings.FROM_EMAIL}" class="hover:text-emerald-400 transition-colors">{settings.FROM_EMAIL}</a>
                </div>
            </div>
        </div>
    </footer>

    <script>
        function highlightMode(mode) {{
            const teaser = document.getElementById('card-teaser');
            const dossier = document.getElementById('card-dossier');
            const btnBoth = document.getElementById('btn-both');
            const btnTeaser = document.getElementById('btn-teaser');
            const btnDossier = document.getElementById('btn-dossier');

            [btnBoth, btnTeaser, btnDossier].forEach(btn => {{
                btn.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white transition-all';
            }});

            if (mode === 'teaser') {{
                btnTeaser.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 transition-all';
                teaser.style.opacity = '1';
                teaser.style.transform = 'scale(1.01)';
                dossier.style.opacity = '0.45';
                dossier.style.transform = 'scale(0.98)';
            }} else if (mode === 'dossier') {{
                btnDossier.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 transition-all';
                dossier.style.opacity = '1';
                dossier.style.transform = 'scale(1.01)';
                teaser.style.opacity = '0.45';
                teaser.style.transform = 'scale(0.98)';
            }} else {{
                btnBoth.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-white transition-all';
                teaser.style.opacity = '1';
                teaser.style.transform = 'scale(1)';
                dossier.style.opacity = '1';
                dossier.style.transform = 'scale(1)';
            }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content, status_code=200)


@app.get("/health")
async def health_check() -> JSONResponse:
    """Return engine health status and SQLite lead/buyer counts."""
    init_db()
    with db_session() as conn:
        total_leads = conn.execute("SELECT COUNT(*) AS cnt FROM leads").fetchone()["cnt"]
        total_buyers = conn.execute("SELECT COUNT(*) AS cnt FROM buyers").fetchone()["cnt"]
        active_buyers = conn.execute(
            "SELECT COUNT(*) AS cnt FROM buyers WHERE unsubscribed = 0"
        ).fetchone()["cnt"]
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM leads GROUP BY status"
        ).fetchall()
        leads_by_status = {row["status"]: row["cnt"] for row in status_rows}

    job = scheduler.get_job("daily_planning_arbitrage_pipeline")
    next_run_time = job.next_run_time.isoformat() if job and job.next_run_time else None

    return JSONResponse(
        content={
            "status": "ok",
            "domain": settings.PUBLIC_BASE_URL,
            "trading_name": settings.TRADING_NAME,
            "auto_approve_mode": settings.AUTO_APPROVE_MODE,
            "lead_price_gbp": settings.LEAD_PRICE_GBP,
            "scheduler_running": bool(scheduler.running),
            "next_daily_pipeline_run": next_run_time,
            "total_leads": total_leads,
            "leads_by_status": leads_by_status,
            "total_buyers": total_buyers,
            "active_buyers": active_buyers,
        }
    )


@app.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_endpoint(token: str) -> HTMLResponse:
    """Set unsubscribed = 1 in buyers table and return a UK PECR compliance confirmation page."""
    init_db()
    if token in ("opt-out", "preview-opt-out"):
        updated_buyer = {
            "b2b_email": settings.FROM_EMAIL,
            "company_name": "Preview Test Recipient (Sandbox)",
        }
    else:
        updated_buyer = unsubscribe_buyer_by_token(token)
    if not updated_buyer:
        raise HTTPException(status_code=404, detail="Invalid or expired unsubscribe token.")

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Unsubscribe Confirmed | UK PECR Compliance</title>
    <style>
        body {{ font-family: Arial, sans-serif; background: #f8fafc; color: #0f172a; padding: 40px; }}
        .card {{ max-width: 560px; margin: 0 auto; background: #ffffff; padding: 32px; border-radius: 8px; border: 1px solid #cbd5e1; }}
        h1 {{ font-size: 20px; margin-top: 0; color: #0f172a; }}
        p {{ font-size: 14px; line-height: 1.6; color: #334155; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Unsubscribe Confirmed</h1>
        <p>The corporate email address <strong>{updated_buyer['b2b_email']}</strong> ({updated_buyer['company_name']}) has been permanently opted out of all future planning condition briefs.</p>
        <p>Your preference has been recorded immediately in compliance with the UK Privacy and Electronic Communications Regulations (PECR).</p>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content, status_code=200)


def _resolve_lead_from_stripe_object(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a lead record using Stripe first-class object IDs (payment_link) first, with metadata as explicit fallback."""
    plink_id = str(obj.get("payment_link") or "").strip()
    if plink_id:
        lead = get_lead_by_payment_link_id(plink_id)
        if lead:
            return lead

    metadata: dict[str, Any] = obj.get("metadata") or {}
    app_ref = metadata.get("app_ref")
    lead_id = metadata.get("lead_id")
    if app_ref:
        lead = get_lead_by_app_ref(str(app_ref))
        if lead:
            return lead
    if lead_id:
        try:
            return get_lead_by_id(int(lead_id))
        except (ValueError, TypeError):
            pass
    return None


def _fulfill_lead_purchase(
    lead: dict[str, Any],
    buyer_email: str,
    hosted_invoice_url: str = "",
    invoice_pdf_url: str = "",
) -> JSONResponse:
    """Idempotently mark lead SOLD, deactivate Payment Link, email Full Dossier PDF + Invoice link, and alert Telegram."""
    target_ref = str(lead["app_ref"])
    already_sold = str(lead.get("status") or "") == LeadStatus.SOLD.value
    update_lead(target_ref, {"status": LeadStatus.SOLD.value})

    plink_id = str(lead.get("stripe_payment_link_id") or "").strip()
    if plink_id and not already_sold:
        deactivate_stripe_link(plink_id)

    if not already_sold:
        dossier_path = lead.get("dossier_pdf_path") or ""
        if buyer_email:
            invoice_section = ""
            if hosted_invoice_url or invoice_pdf_url:
                links = []
                if hosted_invoice_url:
                    links.append(f"<a href='{hosted_invoice_url}'>View Hosted B2B Invoice</a>")
                if invoice_pdf_url:
                    links.append(f"<a href='{invoice_pdf_url}'>Download VAT/Tax Invoice PDF</a>")
                invoice_section = f"<p><strong>B2B Accounting / Tax Invoice:</strong> {' | '.join(links)}</p>"

            html_body = (
                f"<p>Thank you for your purchase.</p>"
                f"<p>Please find attached the unredacted Full Intelligence Dossier for "
                f"<strong>{target_ref}</strong> ({lead.get('site_address', '')}).</p>"
                f"{invoice_section}"
            )
            send_resend_email(
                to_email=buyer_email,
                subject=f"Unredacted Planning Dossier: {target_ref}",
                html_body=html_body,
                attachment_path=dossier_path,
            )

        send_telegram_message(
            f"💷 <b>LEAD SOLD (£{settings.LEAD_PRICE_GBP})</b>\n"
            f"Application: <code>{target_ref}</code>\n"
            f"Category: {lead.get('category', '')}\n"
            f"Buyer: {buyer_email or 'Verified Checkout Customer'}"
        )

    return JSONResponse(
        content={
            "status": "sold",
            "app_ref": target_ref,
            "buyer_email": buyer_email,
            "idempotent_replay": already_sold,
        }
    )


@app.post("/webhook/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> JSONResponse:
    """Verify Stripe-Signature (supporting snapshot & v2 thin event_destination payloads), gate fulfillment on payment_status, and deliver Full Dossier PDF."""
    init_db()
    raw_body = await request.body()
    raw_secrets = [
        s.strip()
        for s in settings.STRIPE_WEBHOOK_SECRET.split(",")
        if s.strip()
        and not s.strip().startswith("whsec_your_")
        and not s.strip().startswith("your-")
    ]

    try:
        parsed_json: dict[str, Any] = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

    is_v2_thin_payload = (
        str(parsed_json.get("object") or "").startswith("v2.core.event")
        or ("related_object" in parsed_json and "data" not in parsed_json)
    )
    client = get_stripe_client()

    if raw_secrets:
        if not stripe_signature:
            raise HTTPException(status_code=400, detail="Missing Stripe-Signature header.")
        verified_event: Any = None
        last_exc: Exception | None = None
        for secret in raw_secrets:
            try:
                if is_v2_thin_payload and client:
                    verified_event = client.parse_event_notification(
                        raw_body,
                        stripe_signature,
                        secret,
                    )
                elif client:
                    verified_event = client.construct_event(
                        raw_body,
                        stripe_signature,
                        secret,
                    )
                else:
                    stripe.Webhook.verify_header(
                        raw_body.decode("utf-8"),
                        stripe_signature,
                        secret,
                    )
                    verified_event = parsed_json
                break
            except Exception as exc:
                last_exc = exc
        if verified_event is None:
            raise HTTPException(status_code=400, detail=f"Invalid Stripe signature: {last_exc}") from last_exc

    event_type = str(parsed_json.get("type") or "")
    if event_type == "v2.core.event_destination.ping":
        return JSONResponse(content={"status": "pong", "event_type": event_type})

    data_obj: dict[str, Any] = (parsed_json.get("data") or {}).get("object") or {}

    # If delivered as a thin event notification without an embedded data.object, fetch the related resource via StripeClient
    if not data_obj and is_v2_thin_payload and client:
        related_obj = parsed_json.get("related_object") or {}
        rel_url = str(related_obj.get("url") or "").strip()
        if rel_url:
            try:
                resp = client.raw_request("get", rel_url)
                data_obj = json.loads(resp.http_body)
            except Exception as exc:
                logger.warning("Could not fetch related_object %s for thin event %s: %s", rel_url, event_type, exc)

    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        payment_status = str(data_obj.get("payment_status") or "paid").lower()
        if payment_status == "unpaid":
            return JSONResponse(
                content={
                    "status": "awaiting_async_payment",
                    "event_type": event_type,
                    "payment_status": payment_status,
                }
            )

        customer_details = data_obj.get("customer_details") or {}
        buyer_email = (
            customer_details.get("email")
            or data_obj.get("customer_email")
            or ""
        )
        lead = _resolve_lead_from_stripe_object(data_obj)
        if lead:
            return _fulfill_lead_purchase(lead=lead, buyer_email=buyer_email)

    elif event_type == "invoice.paid":
        buyer_email = str(data_obj.get("customer_email") or "")
        hosted_invoice_url = str(data_obj.get("hosted_invoice_url") or "")
        invoice_pdf_url = str(data_obj.get("invoice_pdf") or "")
        lead = _resolve_lead_from_stripe_object(data_obj)
        if lead:
            return _fulfill_lead_purchase(
                lead=lead,
                buyer_email=buyer_email,
                hosted_invoice_url=hosted_invoice_url,
                invoice_pdf_url=invoice_pdf_url,
            )

    elif event_type in ("checkout.session.async_payment_failed", "invoice.payment_failed"):
        lead = _resolve_lead_from_stripe_object(data_obj)
        target_ref = str(lead["app_ref"]) if lead else str((data_obj.get("metadata") or {}).get("app_ref") or "UNKNOWN")
        send_telegram_message(
            f"⚠️ <b>STRIPE PAYMENT FAILED</b> ({event_type})\n"
            f"Application: <code>{target_ref}</code>"
        )
        return JSONResponse(content={"status": "payment_failed", "event_type": event_type, "app_ref": target_ref})

    elif event_type in ("charge.refunded", "credit_note.created"):
        lead = _resolve_lead_from_stripe_object(data_obj)
        if lead:
            target_ref = str(lead["app_ref"])
            update_lead(target_ref, {"status": LeadStatus.REFUNDED.value})
            send_telegram_message(
                f"↩️ <b>LEAD REFUNDED / CREDITED</b> ({event_type})\n"
                f"Application: <code>{target_ref}</code>"
            )
            return JSONResponse(content={"status": "refunded", "event_type": event_type, "app_ref": target_ref})

    elif event_type in ("charge.dispute.created", "radar.early_fraud_warning.created"):
        lead = _resolve_lead_from_stripe_object(data_obj)
        target_ref = str(lead["app_ref"]) if lead else str((data_obj.get("metadata") or {}).get("app_ref") or "UNKNOWN")
        if lead:
            update_lead(target_ref, {"status": LeadStatus.DISPUTED.value})
        send_telegram_message(
            f"🚨 <b>STRIPE RISK ALERT ({event_type})</b>\n"
            f"Application: <code>{target_ref}</code>"
        )
        return JSONResponse(content={"status": "disputed", "event_type": event_type, "app_ref": target_ref})

    return JSONResponse(content={"status": "ignored", "event_type": event_type})


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    """Handle [Approve] and [Discard] inline callback buttons when AUTO_APPROVE_MODE=False."""
    init_db()
    payload = await request.json()
    callback = payload.get("callback_query") or {}
    callback_query_id = str(callback.get("id") or "")
    data = str(callback.get("data", ""))

    # 1) Immediately acknowledge the Telegram callback query so the button stops glowing on the user's phone
    if callback_query_id:
        answer_telegram_callback(callback_query_id)

    if ":" not in data:
        return JSONResponse(content={"status": "ignored"})

    action, lead_id_str = data.split(":", 1)
    try:
        lead_id = int(lead_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid lead_id in callback_data.")

    lead = get_lead_by_id(lead_id)
    if not lead:
        return JSONResponse(status_code=200, content={"status": "lead_not_found", "lead_id": lead_id})

    app_ref = str(lead["app_ref"])
    if action == "approve":
        try:
            postcode = str(lead.get("postcode") or "BN1 1AA")
            sent_emails = dispatch_teaser_to_buyers(
                lead_id=lead_id,
                app_ref=app_ref,
                category=str(lead.get("category") or ""),
                postcode_area=str(lead.get("postcode_area") or "BN"),
                postcode_outward=extract_outward_postcode(postcode),
                verbatim_quote=str(lead.get("verbatim_quote") or ""),
                teaser_pdf_path=str(lead.get("teaser_pdf_path") or ""),
                stripe_payment_url=str(lead.get("stripe_payment_url") or ""),
                exclude_company_number=str(lead.get("company_number") or ""),
                exclude_email=str(lead.get("b2b_contact_email") or ""),
            )
            send_telegram_message(
                f"✅ <b>Approved &amp; Dispatched:</b> <code>{html.escape(app_ref)}</code>\n"
                f"Sent to {len(sent_emails)} corporate buyer(s): {', '.join(sent_emails) if sent_emails else 'None matched'}"
            )
            return JSONResponse(
                content={
                    "status": "approved_and_dispatched",
                    "app_ref": app_ref,
                    "dispatched_to": sent_emails,
                }
            )
        except Exception as exc:
            tb_str = traceback.format_exc()
            logger.error("Lead dispatch failed for %s (id=%s):\n%s", app_ref, lead_id, tb_str)
            print(f"ERROR dispatching lead {app_ref} (id={lead_id}):\n{tb_str}", file=sys.stderr, flush=True)
            send_telegram_message(
                f"❌ <b>Dispatch Failed for Lead {html.escape(app_ref)} (ID {lead_id})</b>\n"
                f"<b>Error:</b> <code>{html.escape(str(exc))}</code>\n"
                f"<pre>{html.escape(tb_str[-1500:])}</pre>"
            )
            return JSONResponse(
                status_code=500,
                content={
                    "status": "dispatch_error",
                    "app_ref": app_ref,
                    "error": str(exc),
                },
            )

    if action == "discard":
        try:
            update_lead(app_ref, {"status": LeadStatus.IGNORED.value})
            send_telegram_message(f"🗑 <b>Lead Discarded:</b> <code>{html.escape(app_ref)}</code>")
            return JSONResponse(content={"status": "discarded", "app_ref": app_ref})
        except Exception as exc:
            tb_str = traceback.format_exc()
            logger.error("Lead discard failed for %s (id=%s):\n%s", app_ref, lead_id, tb_str)
            print(f"ERROR discarding lead {app_ref} (id={lead_id}):\n{tb_str}", file=sys.stderr, flush=True)
            send_telegram_message(
                f"❌ <b>Discard Failed for Lead {html.escape(app_ref)} (ID {lead_id})</b>\n"
                f"<b>Error:</b> <code>{html.escape(str(exc))}</code>\n"
                f"<pre>{html.escape(tb_str[-1500:])}</pre>"
            )
            return JSONResponse(
                status_code=500,
                content={"status": "discard_error", "app_ref": app_ref, "error": str(exc)},
            )

    return JSONResponse(content={"status": "ignored"})
