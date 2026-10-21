"""Phase 3: Agent 1 - Playwright Idox Council Portal Scraper."""

import hashlib
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
from playwright. async_api import async_playwright

from app.config import PDFS_RAW_DIR, settings
from app.database import create_or_get_lead, get_lead_by_app_ref, init_db
from app.models import HarvestedPDF

logger = logging.getLogger(__name__)


def sanitize_app_ref(app_ref: str) -> str:
    """Convert application reference like 'BH2026/01842' into safe filename stem 'BH2026_01842'."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", app_ref.strip())


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def register_local_pdf(
    app_ref: str,
    pdf_path: Path,
    source_url: str = "local://pdfs_raw",
    db_path: Optional[str] = None,
) -> Optional[HarvestedPDF]:
    """Compute SHA-256 and register a downloaded Decision Notice PDF in SQLite if not already processed."""
    init_db(db_path)
    if not pdf_path.exists():
        return None

    existing = get_lead_by_app_ref(app_ref, db_path=db_path)
    if existing and existing.get("status") != "NEW":
        logger.info("Skipping already-processed application %s (status=%s)", app_ref, existing.get("status"))
        return None

    sha256_hex = compute_sha256(pdf_path)
    lead_id, _ = create_or_get_lead(app_ref=app_ref, pdf_sha256=sha256_hex, db_path=db_path)
    return HarvestedPDF(
        app_ref=app_ref,
        pdf_path=str(pdf_path.resolve()),
        pdf_sha256=sha256_hex,
        source_url=source_url,
        lead_id=lead_id,
    )


import base64

NON_COMMERCIAL_PREFIXES: tuple[str, ...] = (
    "prior approval for the erection of a single storey rear extension",
    "certificate of lawfulness",
    "non-material amendment",
    "application for approval of details reserved by condition",
    "discharge of condition",
)


async def harvest_council_portal(
    council_url: str,
    output_dir: Optional[Path] = None,
    db_path: Optional[str] = None,
    max_applications: int = 10,
) -> list[HarvestedPDF]:
    """Scrape an Idox weekly decided list via Playwright headless Chromium and download Decision Notice PDFs."""
    init_db(db_path)
    target_dir = output_dir or PDFS_RAW_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    harvested_results: list[HarvestedPDF] = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                locale="en-GB",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = { runtime: {} };"
            )
            page = await context.new_page()
            await page.goto(council_url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(1500)

            decided_selector = "input#dateDecided, input#weekTypeDecided, input[type='radio'][value='DC_Decided']"
            decided_radio = page.locator(decided_selector)
            if await decided_radio.count() > 0:
                await decided_radio.first.check()

            submit_btn = page.locator("input[type='submit'][value='Search'], input.button.primary, input[type='submit'], button[type='submit']")
            if await submit_btn.count() > 0:
                await submit_btn.first.click()
                await page.wait_for_load_state("networkidle", timeout=30000)
                await page.wait_for_timeout(1500)

            search_items = page.locator("li.searchresult")
            count = await search_items.count()

            # If the brand-new week has 0 decided applications yet, select the previous week (index=1)
            if count == 0:
                await page.goto(council_url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(1500)
                decided_radio = page.locator(decided_selector)
                if await decided_radio.count() > 0:
                    await decided_radio.first.check()
                week_select = page.locator("select#week, select[name='week']")
                if await week_select.count() > 0:
                    await week_select.first.select_option(index=1)
                submit_btn = page.locator("input[type='submit'][value='Search'], input.button.primary, input[type='submit'], button[type='submit']")
                if await submit_btn.count() > 0:
                    await submit_btn.first.click()
                    await page.wait_for_load_state("networkidle", timeout=30000)
                    await page.wait_for_timeout(1500)

            # Expand results per page to 100 if available so we can select the best commercial/conditional applications
            rpp_select = page.locator("select#resultsPerPage")
            if await rpp_select.count() > 0:
                try:
                    await rpp_select.first.select_option("100")
                    go_btn = page.locator("input[type='submit'][value='Go']")
                    if await go_btn.count() > 0:
                        await go_btn.first.click()
                        await page.wait_for_load_state("networkidle", timeout=30000)
                        await page.wait_for_timeout(1500)
                except Exception as exc:
                    logger.debug("Could not expand resultsPerPage: %s", exc)

            search_items = page.locator("li.searchresult")
            count = await search_items.count()

            commercial_candidates: list[tuple[str, str]] = []
            fallback_candidates: list[tuple[str, str]] = []

            for idx in range(count):
                item = search_items.nth(idx)
                link_el = item.locator("a").first
                if await link_el.count() == 0:
                    continue
                href = await link_el.get_attribute("href") or ""
                meta_text = " ".join((await item.inner_text()).split())
                if "Status: Withdrawn" in meta_text:
                    continue
                ref_match = re.search(r"\b([A-Z]{1,4}\d{2,4}/\d{3,6}(?:/[A-Z0-9]+)?)\b", meta_text)
                app_ref = ref_match.group(1) if ref_match else f"APP_{idx + 1}"
                if not href:
                    continue
                full_summary_url = urljoin(page.url, href)
                lower_desc = meta_text.lower()
                if lower_desc.startswith(NON_COMMERCIAL_PREFIXES):
                    fallback_candidates.append((app_ref, full_summary_url))
                else:
                    commercial_candidates.append((app_ref, full_summary_url))

            app_links = (commercial_candidates + fallback_candidates)[:max_applications]

            for app_ref, summary_url in app_links:
                existing = get_lead_by_app_ref(app_ref, db_path=db_path)
                if existing and existing.get("status") != "NEW":
                    continue

                try:
                    await page.goto(summary_url, wait_until="networkidle", timeout=25000)
                    await page.wait_for_timeout(800)

                    docs_tab = page.locator("a[href*='activeTab=documents'], a:has-text('Documents')").first
                    if await docs_tab.count() > 0:
                        docs_href = await docs_tab.get_attribute("href")
                        if docs_href:
                            await page.goto(urljoin(page.url, docs_href), wait_until="networkidle", timeout=25000)
                            await page.wait_for_timeout(800)

                    pdf_locator = page.locator(
                        "table#Documents tr:has-text('Decision Notice') a[href*='.pdf'], "
                        "table#Documents tr:has-text('Decision') a[href*='.pdf'], "
                        "tr:has-text('Decision Notice') a[href*='.pdf'], "
                        "tr:has-text('Decision') a[href*='files'], "
                        "a[href*='DECISION'][href*='.pdf'], "
                        "a[href*='.pdf']:has-text('Decision')"
                    ).first
                    if await pdf_locator.count() == 0:
                        continue

                    pdf_href = await pdf_locator.get_attribute("href")
                    if not pdf_href:
                        continue
                    full_pdf_url = urljoin(page.url, pdf_href)

                    b64_data = await page.evaluate(
                        """async (url) => {
                            const resp = await fetch(url, { credentials: "include" });
                            if (!resp.ok) return "";
                            const buf = await resp.arrayBuffer();
                            let binary = "";
                            const bytes = new Uint8Array(buf);
                            for (let i = 0; i < bytes.byteLength; i++) {
                                binary += String.fromCharCode(bytes[i]);
                            }
                            return btoa(binary);
                        }""",
                        full_pdf_url,
                    )
                    pdf_bytes = base64.b64decode(b64_data) if b64_data else b""
                    if pdf_bytes.startswith(b"%PDF"):
                        safe_name = sanitize_app_ref(app_ref)
                        pdf_file = target_dir / f"{safe_name}.pdf"
                        pdf_file.write_bytes(pdf_bytes)
                        registered = register_local_pdf(
                            app_ref=app_ref,
                            pdf_path=pdf_file,
                            source_url=full_pdf_url,
                            db_path=db_path,
                        )
                        if registered:
                            harvested_results.append(registered)
                            logger.info("Downloaded real Decision Notice PDF for %s (%d bytes)", app_ref, len(pdf_bytes))
                except Exception as inner_exc:
                    logger.warning("Failed to download Decision Notice for %s: %s", app_ref, inner_exc)

            await browser.close()
    except Exception as exc:
        logger.warning("Playwright portal harvest error on %s: %s", council_url, exc)

    return harvested_results


async def run_agent1_harvester(
    output_dir: Optional[Path] = None,
    db_path: Optional[str] = None,
) -> list[HarvestedPDF]:
    """Run Agent 1 across all configured council URLs and also pick up any unprocessed PDFs in data/pdfs_raw."""
    init_db(db_path)
    target_dir = output_dir or PDFS_RAW_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    results: list[HarvestedPDF] = []
    seen_refs: set[str] = set()

    for url in settings.COUNCIL_DECISION_URLS:
        portal_pdfs = await harvest_council_portal(url, output_dir=target_dir, db_path=db_path)
        for item in portal_pdfs:
            if item.app_ref not in seen_refs:
                results.append(item)
                seen_refs.add(item.app_ref)

    # Also scan local data/pdfs_raw for any newly dropped PDF files
    for pdf_file in sorted(target_dir.glob("*.pdf")):
        stem = pdf_file.stem
        app_ref = stem.replace("_", "/", 1) if re.match(r"^[A-Z]{1,4}\d{2,4}_\d+", stem) else stem
        if app_ref in seen_refs:
            continue
        registered = register_local_pdf(app_ref=app_ref, pdf_path=pdf_file, db_path=db_path)
        if registered:
            results.append(registered)
            seen_refs.add(app_ref)

    return results
