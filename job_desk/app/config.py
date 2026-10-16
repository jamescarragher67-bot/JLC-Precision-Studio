"""Model config, pricing, cap defaults, and paths.

Nothing in here reads secrets from files. API keys come from the environment
only and are never written to the database, the event log, or any file.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# The dashboard binds to loopback only. There is deliberately no env override.
HOST = "127.0.0.1"
PORT = 8000

FIVERR_URL = "https://www.fiverr.com/"

# --- Models ----------------------------------------------------------------

HAIKU_MODEL = "claude-haiku-5-5"

# PLACEHOLDER: GPT-6 Astra is not configured yet.
# TODO(astra): set this to the real Astra model id once it is chosen. While it
# is None, the build stage refuses to run, marks the job failed with reason
# "builder not configured", and makes no API call.
BUILDER_MODEL: str | None = None

# Prices are USD per million tokens, keyed by model id.
# An empty entry means "no price known": api_client refuses calls to that
# model so the spend cap cannot be bypassed by an unpriced call.
PRICING: dict[str, dict] = {
    HAIKU_MODEL: {
        "input_per_mtok": 0.10,
        "output_per_mtok": 0.50,
        "note": "verify before relying on this",
    },
    # TODO(astra): key this by BUILDER_MODEL and fill in rates once known.
    "GPT-6 Astra (not configured)": {},
}

# --- Caps and limits -------------------------------------------------------

DEFAULT_MONTHLY_API_CAP_USD = 100.0
MAX_STAGE_RETRIES = 2  # retries after the first attempt; 3 attempts in total
MAX_STEPS_PER_JOB_PER_PASS = 20  # guards run_once against runaway loops
MAX_SKETCH_BYTES = 10 * 1024 * 1024
SKETCH_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".pdf", ".dxf", ".svg"})

# --- Typed measurements (job entry form) -----------------------------------

TYPED_TOLERANCE_MM = 0.05
TYPED_MEASUREMENTS = {
    # form field name -> (measurement name, unit)
    "length_mm": ("length", "mm"),
    "width_mm": ("width", "mm"),
    "hole_diameter_mm": ("hole_diameter", "mm"),
}

# --- Fiverr CSV import -----------------------------------------------------
# UNCONFIRMED: these header names are guesses. Replace them with the headers
# from a real Fiverr export once the user provides one. Amounts are assumed
# to be AUD already.
FIVERR_CSV_MAPPING = {
    "date": "Date",
    "description": "Description",
    "amount": "Amount",
    "external_ref": "Transaction ID",
    "date_format": "%Y-%m-%d",
}


# --- Paths -----------------------------------------------------------------

def data_dir() -> Path:
    override = os.environ.get("JOB_DESK_DATA_DIR")
    return Path(override) if override else BASE_DIR / "data"


def db_path() -> Path:
    return data_dir() / "job_desk.sqlite3"


def jobs_root() -> Path:
    override = os.environ.get("JOB_DESK_JOBS_ROOT")
    return Path(override) if override else BASE_DIR / "jobs"


def events_path() -> Path:
    return jobs_root() / "events.log"


# --- Helpers ---------------------------------------------------------------

def price_for(model: str) -> dict | None:
    """Return the price entry for a model, or None if it is missing or empty."""
    entry = PRICING.get(model)
    if not entry or "input_per_mtok" not in entry:
        return None
    return entry


def api_key(name: str) -> str | None:
    """Read an API key from the environment. Callers must not log the value."""
    if name not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        raise ValueError(f"unknown key name: {name}")
    return os.environ.get(name) or None
