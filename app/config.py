"""Phase 1: Application Configuration using Pydantic BaseSettings."""

import json
from pathlib import Path
from typing import Any
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PDFS_RAW_DIR = DATA_DIR / "pdfs_raw"
DOSSIERS_DIR = DATA_DIR / "dossiers_generated"
BUYERS_SEED_FILE = DATA_DIR / "buyers_seed.json"
DEFAULT_DB_PATH = DATA_DIR / "engine.db"


class Settings(BaseSettings):
    """Global settings loaded from environment variables or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    AUTO_APPROVE_MODE: bool = True
    LEAD_PRICE_GBP: int = 95
    COUNCIL_DECISION_URLS: list[str] = [
        "https://publicaccess.brighton-hove.gov.uk/online-applications/search.do?action=weeklyList"
    ]

    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o"

    COMPANIES_HOUSE_API_KEY: str = ""

    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_API_VERSION: str = "2026-09-30.endive"

    RESEND_API_KEY: str = ""
    FROM_EMAIL: str = "alerts@statutoryintel.co.uk"
    PUBLIC_BASE_URL: str = "https://statutoryintel.co.uk"

    TRADING_NAME: str = "Statutory Intel"
    SOLE_TRADER_NAME: str = "S. Howarth"
    SERVICE_ADDRESS: str = "Brighton & Hove, East Sussex, United Kingdom"

    TELEGRAM_BOT_TOKEN: str = "8850890796:AAF7DAaSCiSjv-kXSddeckGuXKl0b1OeNJ8"
    TELEGRAM_CHAT_ID: str = "8931559851"

    DATABASE_PATH: str = str(DEFAULT_DB_PATH)

    @field_validator("COUNCIL_DECISION_URLS", mode="before")
    @classmethod
    def parse_urls(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(item).strip() for item in v if str(item).strip()]
        if isinstance(v, str):
            val = v.strip()
            if val.startswith("["):
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except json.JSONDecodeError:
                    pass
            return [u.strip() for u in val.split(",") if u.strip()]
        return [
            "https://publicaccess.brighton-hove.gov.uk/online-applications/search.do?action=weeklyList"
        ]


def ensure_directories() -> None:
    """Ensure all required data directories exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDFS_RAW_DIR.mkdir(parents=True, exist_ok=True)
    DOSSIERS_DIR.mkdir(parents=True, exist_ok=True)


ensure_directories()
settings = Settings()
