# UK Planning Condition & Refusal Arbitrage Engine (Agents 0 to 6)

Autonomous B2B lead intelligence and arbitrage engine that monitors UK Council Planning Decision Notices, extracts mandatory statutory pre-commencement conditions and refusal triggers, verifies UK corporate status via Companies House (PECR Regulation 22 compliant), synthesizes redacted Teaser and unredacted Full Dossier PDFs, and dispatches single-use £95 Stripe Payment Links to specialist consultancies.

## Architecture (Agents 0 to 6)
- **Agent 0 (`app/seed_buyers.py`)**: Queries UK Companies House API across 7 specialist consultancy categories, filters strictly for active `ltd`, `llp`, and `plc` entities, scrapes corporate role emails (`enquiries@`, `info@`, `projects@`, `tenders@`, `office@`, `hello@`), and seeds `data/buyers_seed.json` + SQLite `buyers`.
- **Agent 1 (`app/agents/agent1_harvester.py`)**: Playwright headless Chromium scraper for Idox weekly decided lists; downloads Decision Notice PDFs to `data/pdfs_raw/` and deduplicates via SHA-256.
- **Agent 2 (`app/agents/agent2_parser.py`)**: Extracts raw text via PyMuPDF (`fitz`) and parses statutory conditions (`ACOUSTIC_BS4142`, `ECOLOGY_BATS_BNG`, `ARBORICULTURAL`, `CONTAMINATED_LAND`, `FLOOD_DRAINAGE`, `HERITAGE_ARCHAEOLOGY`, `PLANNING_APPEAL`) using OpenAI Structured Outputs with deterministic fallback.
- **Agent 3 (`app/agents/agent3_verifier.py`)**: Verifies applicant/agent corporate status via Companies House API and resolves B2B contact emails, rejecting private homeowner applications.
- **Agent 4 (`app/agents/agent4_synthesizer.py`)**: Generates `teaser_{app_ref}.pdf` (redacted) and `dossier_{app_ref}.pdf` (unredacted full intelligence dossier) via ReportLab.
- **Agent 5 (`app/agents/agent5_critic.py`)**: Deterministic hallucination guardrail (`difflib.SequenceMatcher` >= 0.90 verbatim quote match) and UK PECR corporate email domain check.
- **Agent 6 (`app/agents/agent6_closer.py`)**: Creates single-use Stripe Payment Links (`completed_sessions.limit = 1`) and dispatches teaser briefs via Resend (or sends Telegram approval cards when `AUTO_APPROVE_MODE=False`).

## Local Setup & Testing
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.seed_buyers
python -m tests.create_mock_pdf
pytest -v
```

## 24/7 VPS Deployment via Docker Compose
```bash
docker compose up -d --build
```
