"""Phase 1: SQLite3 Database Setup in WAL mode."""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import settings


def get_db_path(custom_path: Optional[str] = None) -> str:
    """Resolve database path and ensure parent directory exists."""
    path_str = custom_path or settings.DATABASE_PATH
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Create a SQLite3 connection configured with WAL mode and Row factory."""
    resolved = get_db_path(db_path)
    conn = sqlite3.connect(resolved, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def db_session(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a SQLite connection with automatic commit/rollback."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite database tables in WAL mode."""
    with db_session(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS buyers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                company_number TEXT UNIQUE NOT NULL,
                company_type TEXT NOT NULL,
                category TEXT NOT NULL,
                postcode_area TEXT NOT NULL,
                full_postcode TEXT NOT NULL,
                website TEXT,
                b2b_email TEXT UNIQUE NOT NULL,
                unsubscribe_token TEXT UNIQUE NOT NULL,
                unsubscribed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_ref TEXT UNIQUE NOT NULL,
                pdf_sha256 TEXT NOT NULL,
                site_address TEXT,
                postcode TEXT,
                postcode_area TEXT,
                decision_type TEXT,
                category TEXT,
                verbatim_quote TEXT,
                applicant_name TEXT,
                company_number TEXT,
                b2b_contact_email TEXT,
                teaser_pdf_path TEXT,
                dossier_pdf_path TEXT,
                stripe_payment_link_id TEXT,
                stripe_payment_url TEXT,
                status TEXT NOT NULL DEFAULT 'NEW',
                created_at TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                buyer_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                resend_message_id TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id),
                FOREIGN KEY (buyer_id) REFERENCES buyers (id)
            );
            """
        )


def upsert_buyer(buyer_data: dict[str, Any], db_path: Optional[str] = None) -> int:
    """Insert or update a PECR-verified corporate buyer record."""
    now = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO buyers (
                company_name, company_number, company_type, category,
                postcode_area, full_postcode, website, b2b_email,
                unsubscribe_token, unsubscribed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_number) DO UPDATE SET
                company_name = excluded.company_name,
                company_type = excluded.company_type,
                category = excluded.category,
                postcode_area = excluded.postcode_area,
                full_postcode = excluded.full_postcode,
                website = excluded.website
            """,
            (
                buyer_data["company_name"],
                buyer_data["company_number"],
                buyer_data["company_type"],
                buyer_data["category"],
                buyer_data["postcode_area"],
                buyer_data["full_postcode"],
                buyer_data.get("website", ""),
                buyer_data["b2b_email"],
                buyer_data["unsubscribe_token"],
                int(buyer_data.get("unsubscribed", 0)),
                buyer_data.get("created_at", now),
            ),
        )
        row = conn.execute(
            "SELECT id FROM buyers WHERE company_number = ?",
            (buyer_data["company_number"],),
        ).fetchone()
        return int(row["id"]) if row else int(cursor.lastrowid or 0)


def create_or_get_lead(
    app_ref: str,
    pdf_sha256: str,
    db_path: Optional[str] = None,
) -> tuple[int, bool]:
    """Create a NEW lead record or return existing (lead_id, is_new)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM leads WHERE app_ref = ?",
            (app_ref,),
        ).fetchone()
        if existing:
            return int(existing["id"]), False

        cursor = conn.execute(
            """
            INSERT INTO leads (app_ref, pdf_sha256, status, created_at)
            VALUES (?, ?, 'NEW', ?)
            """,
            (app_ref, pdf_sha256, now),
        )
        return int(cursor.lastrowid or 0), True


def normalize_site_address(site_address: str) -> str:
    """Normalize site address for deterministic site + category + company deduplication."""
    return re.sub(r"[^a-z0-9]+", " ", (site_address or "").strip().lower()).strip()


def update_lead(
    app_ref: str,
    updates: dict[str, Any],
    db_path: Optional[str] = None,
) -> None:
    """Update columns on a lead identified by app_ref (or merged primary app_ref prefix)."""
    if not updates:
        return
    columns = ", ".join(f"{k} = ?" for k in updates.keys())
    with db_session(db_path) as conn:
        cur = conn.execute(
            f"UPDATE leads SET {columns} WHERE app_ref = ?",
            list(updates.values()) + [app_ref],
        )
        if cur.rowcount == 0:
            conn.execute(
                f"UPDATE leads SET {columns} WHERE app_ref LIKE ?",
                list(updates.values()) + [f"{app_ref} & %"],
            )


def get_lead_by_app_ref(app_ref: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Fetch a lead row as a dictionary by application reference (supporting merged primary refs)."""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT * FROM leads WHERE app_ref = ?", (app_ref,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM leads WHERE app_ref LIKE ? ORDER BY id ASC LIMIT 1",
                (f"{app_ref} & %",),
            ).fetchone()
        return dict(row) if row else None


def find_duplicate_primary_lead(
    app_ref: str,
    site_address: str,
    category: str,
    company_number: str,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Find an existing primary lead with the same normalized site_address, category, and company_number."""
    norm_addr = normalize_site_address(site_address)
    clean_cat = (category or "").strip()
    clean_num = (company_number or "").strip().upper()
    if not norm_addr or not clean_cat or not clean_num:
        return None

    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM leads
            WHERE category = ?
              AND UPPER(TRIM(company_number)) = ?
              AND status NOT IN ('IGNORED', 'REJECTED_NON_COMMERCIAL', 'CRITIC_REJECTED', 'MERGED_DUPLICATE')
            ORDER BY id ASC
            """,
            (clean_cat, clean_num),
        ).fetchall()
        for row in rows:
            row_dict = dict(row)
            row_ref = str(row_dict.get("app_ref") or "")
            base_ref = row_ref.split(" & ")[0].strip()
            if row_ref == app_ref or base_ref == app_ref:
                continue
            if normalize_site_address(str(row_dict.get("site_address") or "")) == norm_addr:
                return row_dict
    return None


def get_lead_by_id(lead_id: int, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Fetch a lead row as a dictionary by ID."""
    with db_session(db_path) as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def get_lead_by_payment_link_id(
    plink_id: str,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Fetch a lead row by first-class Stripe Payment Link ID (stripe_payment_link_id)."""
    clean_plink = (plink_id or "").strip()
    if not clean_plink:
        return None
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE stripe_payment_link_id = ? ORDER BY id ASC LIMIT 1",
            (clean_plink,),
        ).fetchone()
        return dict(row) if row else None


def get_matching_buyers(
    category: str,
    postcode_area: Optional[str] = None,
    exclude_company_number: Optional[str] = None,
    exclude_email: Optional[str] = None,
    limit: int = 5,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` unsubscribed=0 buyers matching category, prioritizing local Sussex & South East postcode_area and enforcing self-loop exclusion."""
    clean_ex_num = (exclude_company_number or "").strip().upper()
    clean_ex_email = (exclude_email or "").strip().lower()

    with db_session(db_path) as conn:
        if postcode_area:
            rows = conn.execute(
                """
                SELECT * FROM buyers
                WHERE category = ?
                  AND unsubscribed = 0
                  AND (? = '' OR UPPER(TRIM(company_number)) != ?)
                  AND (? = '' OR LOWER(TRIM(b2b_email)) != ?)
                ORDER BY
                    CASE
                        WHEN postcode_area = ? THEN 0
                        WHEN postcode_area IN ('BN', 'RH', 'TN', 'PO', 'GU', 'KT', 'CR', 'BR', 'ME', 'CT') THEN 1
                        ELSE 2
                    END,
                    id ASC
                LIMIT ?
                """,
                (category, clean_ex_num, clean_ex_num, clean_ex_email, clean_ex_email, postcode_area, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM buyers
                WHERE category = ?
                  AND unsubscribed = 0
                  AND (? = '' OR UPPER(TRIM(company_number)) != ?)
                  AND (? = '' OR LOWER(TRIM(b2b_email)) != ?)
                ORDER BY
                    CASE
                        WHEN postcode_area IN ('BN', 'RH', 'TN', 'PO', 'GU', 'KT', 'CR', 'BR', 'ME', 'CT') THEN 0
                        ELSE 1
                    END,
                    id ASC
                LIMIT ?
                """,
                (category, clean_ex_num, clean_ex_num, clean_ex_email, clean_ex_email, limit),
            ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if clean_ex_num and str(d.get("company_number") or "").strip().upper() == clean_ex_num:
                continue
            if clean_ex_email and str(d.get("b2b_email") or "").strip().lower() == clean_ex_email:
                continue
            results.append(d)
        return results


def record_dispatch(
    lead_id: int,
    buyer_id: int,
    resend_message_id: str,
    db_path: Optional[str] = None,
) -> int:
    """Insert a dispatch record into dispatches table."""
    now = datetime.now(timezone.utc).isoformat()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO dispatches (lead_id, buyer_id, sent_at, resend_message_id)
            VALUES (?, ?, ?, ?)
            """,
            (lead_id, buyer_id, now, resend_message_id),
        )
        return int(cursor.lastrowid or 0)


def unsubscribe_buyer_by_token(token: str, db_path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Mark a buyer as unsubscribed = 1 by unsubscribe_token and return the updated buyer."""
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM buyers WHERE unsubscribe_token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE buyers SET unsubscribed = 1 WHERE unsubscribe_token = ?",
            (token,),
        )
        updated = conn.execute(
            "SELECT * FROM buyers WHERE unsubscribe_token = ?",
            (token,),
        ).fetchone()
        return dict(updated) if updated else None
