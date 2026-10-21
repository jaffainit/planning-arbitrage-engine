# SYSTEM SPECIFICATION: AUTONOMOUS BUILD & VPS DEPLOYMENT
# PROJECT: UK Planning Condition & Refusal Arbitrage Engine (Agents 0 to 6)

## 1. AUTONOMOUS EXECUTION RULES
You are operating in fully autonomous engineering mode on a Linux VPS.
- DO NOT stop to ask clarifying questions. Make deterministic architectural decisions based on this spec.
- DO NOT write placeholder code, `pass` statements, `TODO` comments, or mock stubs inside production modules. Write complete, working implementations for every file.
- Execute this build in strict numerical order (Phase 1 through Phase 5).
- Run your own terminal commands to create a virtual environment, install dependencies, generate sample test PDFs, run `pytest`, and fix any tracebacks until 100% of tests pass.
- Package the final application with `Dockerfile` and `docker-compose.yml` configured with `restart: unless-stopped` so it runs 24/7 on this VPS.

---

## 2. TARGET DIRECTORY STRUCTURE
Create and populate this exact file tree:

    planning-arbitrage-engine/
    ├── .env.example
    ├── Dockerfile
    ├── docker-compose.yml
    ├── requirements.txt
    ├── README.md
    ├── data/
    │   ├── pdfs_raw/
    │   ├── dossiers_generated/
    │   └── buyers_seed.json
    ├── app/
    │   ├── __init__.py
    │   ├── config.py              # Phase 1: Pydantic BaseSettings loading .env
    │   ├── database.py            # Phase 1: SQLite WAL setup (leads, buyers, dispatches, payments)
    │   ├── models.py              # Phase 1: Pydantic v2 schemas for strict inter-agent handoffs
    │   ├── seed_buyers.py         # Phase 2: Agent 0 - PECR-compliant UK B2B Buyer Harvester
    │   ├── orchestrator.py        # Phase 4: Sequential runner connecting Agents 1 -> 2 -> 3 -> 4 -> 5 -> 6
    │   ├── main.py                # Phase 4: FastAPI server (Stripe/Telegram/Unsubscribe endpoints + APScheduler)
    │   └── agents/
    │       ├── __init__.py
    │       ├── agent1_harvester.py    # Phase 3: Playwright Idox council portal scraper
    │       ├── agent2_parser.py       # Phase 3: PyMuPDF extractor + LLM JSON condition parser
    │       ├── agent3_verifier.py     # Phase 3: Companies House API + corporate email resolver
    │       ├── agent4_synthesizer.py  # Phase 3: ReportLab PDF generator (Teaser + Full Dossier)
    │       ├── agent5_critic.py       # Phase 3: Verbatim quote matcher + UK PECR guardrail
    │       └── agent6_closer.py       # Phase 3: Stripe Payment Links + Resend + Telegram
    └── tests/
        ├── __init__.py
        ├── create_mock_pdf.py     # Phase 5: Generates realistic Council Decision Notice PDF
        └── test_pipeline.py       # Phase 5: End-to-end pytest suite for all agents & webhooks

---

## 3. CHRONOLOGICAL BUILD PHASES

### PHASE 1: Foundation (`requirements.txt`, `config.py`, `database.py`, `models.py`)
1. **`requirements.txt`**: Include `fastapi`, `uvicorn`, `apscheduler`, `pydantic`, `pydantic-settings`, `python-dotenv`, `httpx`, `beautifulsoup4`, `playwright`, `pymupdf`, `reportlab`, `openai`, `stripe`, `pytest`, `pytest-asyncio`.
2. **`app/config.py`**: Load environment variables with sensible defaults, including `AUTO_APPROVE_MODE: bool = True`, `LEAD_PRICE_GBP: int = 95`, `COUNCIL_DECISION_URLS: list[str]` (defaulting to Brighton & Hove City Council Idox weekly decided list URL), `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `COMPANIES_HOUSE_API_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `RESEND_API_KEY`, `FROM_EMAIL`, `PUBLIC_BASE_URL`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID`.
3. **`app/database.py`**: Configure SQLite3 in `WAL` mode (`data/engine.db`). Create 3 tables on startup:
   - `buyers`: `id`, `company_name`, `company_number` (UNIQUE), `company_type`, `category`, `postcode_area`, `full_postcode`, `website`, `b2b_email` (UNIQUE), `unsubscribe_token` (UNIQUE), `unsubscribed` (INTEGER DEFAULT 0), `created_at`.
   - `leads`: `id`, `app_ref` (UNIQUE), `pdf_sha256`, `site_address`, `postcode`, `postcode_area`, `decision_type`, `category`, `verbatim_quote`, `applicant_name`, `company_number`, `b2b_contact_email`, `teaser_pdf_path`, `dossier_pdf_path`, `stripe_payment_link_id`, `stripe_payment_url`, `status` (NEW, IGNORED, REJECTED_NON_COMMERCIAL, CRITIC_REJECTED, PENDING_APPROVAL, DISPATCHED, SOLD), `created_at`.
   - `dispatches`: `id`, `lead_id`, `buyer_id`, `sent_at`, `resend_message_id`.
4. **`app/models.py`**: Define strict Pydantic v2 models for every agent's input and output.

---

### PHASE 2: Agent 0 — Automated B2B Buyer Harvester (`app/seed_buyers.py`)
Write `app/seed_buyers.py` so it can be run via `python -m app.seed_buyers` and also called programmatically:
1. Query the UK Companies House API (`https://api.company-information.service.gov.uk/search/companies`) across these 7 specialist mappings:
   - `"acoustic consultancy"` -> `ACOUSTIC_BS4142`
   - `"ecological consultancy"` -> `ECOLOGY_BATS_BNG`
   - `"arboricultural consultancy"` -> `ARBORICULTURAL`
   - `"geo-environmental consultancy"` -> `CONTAMINATED_LAND`
   - `"flood risk consultancy"` -> `FLOOD_DRAINAGE`
   - `"heritage consultancy"` -> `HERITAGE_ARCHAEOLOGY`
   - `"town planning consultancy"` -> `PLANNING_APPEAL`
2. **UK PECR Regulation 22 Guardrail:** Strictly filter for `company_status == "active"` and `company_type` in `["ltd", "llp", "plc"]`. Never store sole traders or unincorporated partnerships.
3. Extract the UK postcode area prefix (e.g., `BN`, `RH`, `TN`, `PO`, `GU`, `SO`, `CT`, `ME`, `KT`, `SW`, `SE`) from `address.postal_code`.
4. Resolve each company's official corporate website via DuckDuckGo Lite HTML search (`https://lite.duckduckgo.com/lite/`), scrape `/`, `/contact`, and `/contact-us` using `httpx` and `BeautifulSoup`, and extract corporate role emails (`enquiries@`, `info@`, `projects@`, `tenders@`, `office@`, `hello@`).
5. Reject any free/ISP email domains (`gmail.com`, `yahoo.com`, `hotmail.com`, `outlook.com`, `btinternet.com`, `sky.com`, `icloud.com`, `live.co.uk`).
6. Generate a UUID4 `unsubscribe_token` for each verified buyer, save all records to `data/buyers_seed.json`, and upsert them into the `buyers` SQLite table. Also include a fallback seed generator so if `COMPANIES_HOUSE_API_KEY` is not yet set during local testing, it seeds test records cleanly.

---

### PHASE 3: Agents 1 to 6 (`app/agents/`)
Build each agent module with full error handling:

1. **`agent1_harvester.py` (Idox Scraper):**
   - Uses Playwright headless Chromium to open the configured council weekly decided list URL, locate each application's Decision Notice PDF, and download it to `data/pdfs_raw/{app_ref}.pdf`.
   - Computes SHA-256 hash and checks `leads` table in SQLite. Skips already-processed `app_ref` records.

2. **`agent2_parser.py` (Statutory Condition & Refusal Extractor):**
   - Extracts raw text from the PDF via `pymupdf` (`fitz`).
   - Calls the LLM using OpenAI Structured Outputs / JSON mode to populate: `application_ref`, `site_address`, `postcode`, `decision_type` (`CONDITIONAL_APPROVAL`, `REFUSAL`, or `UNCONDITIONAL_OR_IRRELEVANT`), `applicant_name`, `agent_company_name`, `agent_email_on_form`, and `specialist_triggers` (each containing `category`, `condition_number`, `verbatim_quote`, and `pre_commencement`).
   - If `UNCONDITIONAL_OR_IRRELEVANT` or no triggers found, marks lead `IGNORED` in SQLite.

3. **`agent3_verifier.py` (Companies House & B2B Resolver):**
   - Queries Companies House API for `agent_company_name` or `applicant_name`.
   - Confirms `company_status == "active"` and corporate entity type (`ltd`, `llp`, `plc`).
   - Resolves corporate B2B contact email. Marks non-commercial/homeowner applications as `REJECTED_NON_COMMERCIAL`.

4. **`agent4_synthesizer.py` (PDF Dossier Builder):**
   - Uses `reportlab` to generate two PDFs in `data/dossiers_generated/`:
     - `teaser_{app_ref}.pdf`: Shows postcode outward code (e.g., `BN1`), required specialist `category`, `verbatim_quote` of the condition, confirmation of active UK Ltd status, and estimated survey contract value. Redacts street address, application reference, and company names.
     - `dossier_{app_ref}.pdf`: Shows full unredacted `application_ref`, `site_address`, company registration numbers, verified B2B contact email, verbatim council conditions, and a ready-to-send outreach email template for the buyer.

5. **`agent5_critic.py` (Deterministic & Legal Guardrail):**
   - **Hallucination Check:** Uses `difflib.SequenceMatcher` (and normalized whitespace substring check) to confirm `verbatim_quote` exists in the raw PDF text with similarity >= 0.90.
   - **PECR Check:** Verifies target email is not on a consumer ISP domain and corporate entity status is confirmed.
   - Marks failing leads as `CRITIC_REJECTED`.

6. **`agent6_closer.py` (Stripe Payment Link + Resend + Telegram):**
   - Creates a Stripe Price (`LEAD_PRICE_GBP * 100` pence, currency `gbp`) and a Stripe Payment Link with `restrictions={"completed_sessions": {"limit": 1}}` and `metadata={"lead_id": str(lead_id), "app_ref": app_ref}`.
   - If `AUTO_APPROVE_MODE == True`, queries `buyers` table for matching `category` and `unsubscribed == 0`, and emails up to 5 local specialists via Resend API with `teaser_{app_ref}.pdf` attached, the Stripe Payment Link, and the mandatory PECR unsubscribe footer link (`{PUBLIC_BASE_URL}/unsubscribe/{unsubscribe_token}`).
   - If `AUTO_APPROVE_MODE == False`, sends a Telegram message with inline buttons (`Approve` / `Discard`) before dispatching emails.

---

### PHASE 4: Orchestrator, Webhooks & Scheduler (`app/orchestrator.py`, `app/main.py`)
1. **`app/orchestrator.py`**: Runs Agents 1 -> 2 -> 3 -> 4 -> 5 -> 6 sequentially for each new PDF, logging every state transition to SQLite.
2. **`app/main.py`**: FastAPI application with:
   - `APScheduler` background cron job running `orchestrator.run_daily_pipeline()` every day at `02:00` (`Europe/London`).
   - `POST /webhook/stripe`: Verifies `Stripe-Signature`, listens for `checkout.session.completed`, marks lead `SOLD` in SQLite, emails the unredacted `dossier_{app_ref}.pdf` to the buyer via Resend, and sends a payout alert to Telegram.
   - `POST /webhook/telegram`: Handles `[Approve]` / `[Discard]` button callbacks when `AUTO_APPROVE_MODE=False`.
   - `GET /unsubscribe/{token}`: Sets `unsubscribed = 1` in the `buyers` table and returns a clear confirmation page for UK PECR compliance.
   - `GET /health`: Returns JSON status and lead counts.

---

### PHASE 5: Self-Testing & Docker Packaging
1. Write `tests/create_mock_pdf.py` using `reportlab` to create a realistic Brighton & Hove Council Decision Notice PDF (`BH2026/01842`) with a pre-commencement BS 4142 acoustic survey condition and an active Ltd architect (`South Coast Design Ltd`).
2. Write `tests/test_pipeline.py` testing:
   - Database initialization and Agent 0 buyer seeding.
   - Agent 2 parsing on the mock PDF.
   - Agent 4 generation of both `teaser_BH2026_01842.pdf` and `dossier_BH2026_01842.pdf`.
   - Agent 5 Critic passing valid verbatim quotes and rejecting hallucinated quotes or `@gmail.com` emails.
   - FastAPI `/unsubscribe/{token}` and `/health` endpoints.
3. Execute `pytest` in a local `.venv` and fix all errors until 100% of tests pass.
4. Create `Dockerfile` (installing Playwright Chromium system dependencies) and `docker-compose.yml` (mapping port `8000:8000` and volume `./data:/app/data` with `restart: unless-stopped`).
