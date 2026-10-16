"""Finance: manual entries, Fiverr CSV import, and monthly totals.

Amounts are AUD and stored as REAL, rounded to cents. API spend is in USD and is
shown separately; it is not added to the totals until an FX rate is decided.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app import config
from app.db import current_month
from app.jobs import JobDeskError

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
KINDS = ("in", "out")


class FinanceInputError(JobDeskError):
    """Bad finance input. The dashboard shows the message to the operator."""


def valid_month(month: str | None) -> str:
    if month is None:
        return current_month()
    if not MONTH_RE.match(month):
        raise FinanceInputError("month must look like YYYY-MM")
    return month


def add_entry(
    conn: sqlite3.Connection,
    *,
    entry_date: str,
    kind: str,
    category: str,
    description: str,
    amount_aud: float,
    source: str = "manual",
    external_ref: str | None = None,
) -> bool:
    """Insert one entry. Returns False if external_ref already exists."""
    if kind not in KINDS:
        raise FinanceInputError("kind must be 'in' or 'out'")
    try:
        date.fromisoformat(entry_date)
    except (TypeError, ValueError) as exc:
        raise FinanceInputError("date must be YYYY-MM-DD") from exc
    amount = round(float(amount_aud), 2)
    if amount < 0:
        raise FinanceInputError("amount must be zero or more; use kind to show direction")
    try:
        conn.execute(
            "INSERT INTO finance_entries"
            " (date, kind, category, description, amount_aud, source, external_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry_date, kind, category.strip() or "uncategorised",
             description.strip(), amount, source, external_ref),
        )
    except sqlite3.IntegrityError as exc:
        # Only a duplicate external_ref is a normal skip. Anything else is a bug.
        if "finance_entries.external_ref" in str(exc):
            return False
        raise
    return True


def list_entries(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM finance_entries WHERE substr(date, 1, 7) = ? ORDER BY date, id",
        (month,),
    ).fetchall()


def totals(conn: sqlite3.Connection, month: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT kind, COALESCE(SUM(amount_aud), 0) AS total FROM finance_entries"
        " WHERE substr(date, 1, 7) = ? GROUP BY kind",
        (month,),
    ).fetchall()
    by_kind = {r["kind"]: round(float(r["total"]), 2) for r in rows}
    money_in = by_kind.get("in", 0.0)
    money_out = by_kind.get("out", 0.0)
    return {"in": money_in, "out": money_out, "net": round(money_in - money_out, 2)}


@dataclass
class ImportResult:
    added: int = 0
    duplicates: int = 0
    rejected: list[str] = field(default_factory=list)


def _parse_amount(raw: str) -> Decimal:
    text = raw.strip().replace("$", "").replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    value = Decimal(text)
    return -value if negative else value


def import_fiverr_csv(
    conn: sqlite3.Connection,
    text: str,
    mapping: dict[str, str] | None = None,
) -> ImportResult:
    """Import a Fiverr CSV using the column mapping in config.

    The mapping is UNCONFIRMED. Positive amounts become 'in' and negative ones
    become 'out'. Rows whose external_ref already exists are skipped.
    """
    mapping = mapping or config.FIVERR_CSV_MAPPING
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    needed = {k: mapping[k] for k in ("date", "description", "amount")}
    missing = [col for col in needed.values() if col not in headers]
    if missing:
        raise FinanceInputError(
            f"CSV is missing expected columns {missing}; found {headers}. "
            "Update FIVERR_CSV_MAPPING in app/config.py to match the export."
        )

    result = ImportResult()
    ref_col = mapping.get("external_ref")
    has_ref = bool(ref_col) and ref_col in headers
    for line_no, row in enumerate(reader, start=2):
        try:
            parsed_date = datetime.strptime(
                row[needed["date"]].strip(), mapping["date_format"]
            ).date().isoformat()
            amount = _parse_amount(row[needed["amount"]])
        except (ValueError, InvalidOperation, KeyError, AttributeError) as exc:
            result.rejected.append(f"line {line_no}: {exc.__class__.__name__}")
            continue
        ext = (row.get(ref_col) or "").strip() if has_ref else ""
        inserted = add_entry(
            conn,
            entry_date=parsed_date,
            kind="in" if amount >= 0 else "out",
            category="fiverr",
            description=(row.get(needed["description"]) or "").strip(),
            amount_aud=abs(float(amount)),
            source="fiverr_csv",
            external_ref=ext or None,
        )
        if inserted:
            result.added += 1
        else:
            result.duplicates += 1
    return result
