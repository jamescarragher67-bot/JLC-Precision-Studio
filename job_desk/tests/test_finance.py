"""Finance: monthly totals, manual entries, and the Fiverr CSV import with dedupe."""

import pytest

from app import finance
from app.finance import FinanceInputError, add_entry, import_fiverr_csv, totals

CSV_TEXT = """Date,Description,Amount,Transaction ID
2026-10-02,Logo order,"$1,200.00",TX-1
2026-10-05,Fiverr fee,"-$12.50",TX-2
2026-10-06,Bad row,notanumber,TX-3
"""


def test_monthly_totals_only_count_the_requested_month(conn):
    add_entry(conn, entry_date="2026-10-01", kind="in", category="client",
              description="job A", amount_aud=100.00)
    add_entry(conn, entry_date="2026-10-20", kind="out", category="software",
              description="CAD licence", amount_aud=30.25)
    add_entry(conn, entry_date="2026-10-30", kind="in", category="client",
              description="job B", amount_aud=50.10)
    add_entry(conn, entry_date="2026-09-30", kind="in", category="client",
              description="last month", amount_aud=999.00)

    assert totals(conn, "2026-10") == {"in": 150.10, "out": 30.25, "net": 119.85}
    assert totals(conn, "2026-09") == {"in": 999.0, "out": 0.0, "net": 999.0}
    assert totals(conn, "2026-01") == {"in": 0.0, "out": 0.0, "net": 0.0}


@pytest.mark.parametrize("kwargs,message", [
    ({"kind": "sideways"}, "kind"),
    ({"amount_aud": -1}, "zero or more"),
    ({"entry_date": "31/10/2026"}, "YYYY-MM-DD"),
])
def test_manual_entry_validation(conn, kwargs, message):
    args = {"entry_date": "2026-10-01", "kind": "in", "category": "x",
            "description": "y", "amount_aud": 1}
    args.update(kwargs)
    with pytest.raises(FinanceInputError, match=message):
        add_entry(conn, **args)


def test_csv_import_parses_signs_and_skips_duplicates(conn):
    first = import_fiverr_csv(conn, CSV_TEXT)
    assert first.added == 2
    assert first.duplicates == 0
    assert len(first.rejected) == 1 and "line 4" in first.rejected[0]

    second = import_fiverr_csv(conn, CSV_TEXT)
    assert second.added == 0
    assert second.duplicates == 2

    assert totals(conn, "2026-10") == {"in": 1200.00, "out": 12.50, "net": 1187.50}
    kinds = {r["external_ref"]: r["kind"] for r in finance.list_entries(conn, "2026-10")}
    assert kinds == {"TX-1": "in", "TX-2": "out"}


def test_csv_with_unexpected_headers_is_refused_with_guidance(conn):
    with pytest.raises(FinanceInputError, match="missing expected columns"):
        import_fiverr_csv(conn, "When,Note,Total\n2026-10-02,x,5\n")


def test_manual_entries_can_be_mixed_with_csv_rows(conn):
    add_entry(conn, entry_date="2026-10-03", kind="in", category="manual",
              description="cash job", amount_aud=80)
    import_fiverr_csv(conn, CSV_TEXT)
    sources = sorted(r["source"] for r in finance.list_entries(conn, "2026-10"))
    assert sources == ["fiverr_csv", "fiverr_csv", "manual"]
    assert totals(conn, "2026-10")["in"] == 1280.00
