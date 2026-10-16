"""Dashboard smoke tests. The app is bound to 127.0.0.1, so the test client uses
that Host header. A different Host is rejected.
"""

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from tests.helpers import BRACKET_ARGS


@pytest.fixture
def client(home):
    return TestClient(app, base_url="http://127.0.0.1")


def _form(**overrides):
    data = {
        "client": BRACKET_ARGS["client"],
        "fiverr_order_no": "FO-WEB-1",
        "order_text": BRACKET_ARGS["order_text"],
        "length_mm": "120",
        "width_mm": "40",
        "hole_diameter_mm": "5.5",
        "price": "45",
        "deadline": "2026-11-01",
    }
    data.update(overrides)
    return data


def test_dashboard_is_meant_for_loopback_only():
    assert config.HOST == "127.0.0.1"


def test_unknown_host_header_is_rejected(home):
    evil = TestClient(app, base_url="http://evil.example")
    assert evil.get("/entry").status_code == 400


def test_entry_page_has_header_controls_and_fiverr_link(client):
    r = client.get("/entry")
    assert r.status_code == 200
    assert "Pause new jobs" in r.text
    assert "API spend" in r.text
    assert 'href="https://www.fiverr.com/" target="_blank" rel="noopener noreferrer"' in r.text


def test_creating_a_job_redirects_to_its_detail_page(client, home):
    r = client.post(
        "/entry",
        data=_form(),
        files={"sketch": ("side view.png", b"\x89PNG fake", "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/jobs/J-0001"
    page = client.get("/jobs/J-0001")
    assert page.status_code == 200
    assert "intake" in page.text
    assert (home.jobs / "J-0001" / "inputs" / "side_view.png").exists()


def test_bad_form_input_shows_the_operator_message(client):
    r = client.post("/entry", data=_form(length_mm="0"))
    assert r.status_code == 400
    assert "length must be greater than zero" in r.text


def test_kill_switch_toggles_and_redirects_only_to_local_paths(client):
    r = client.post("/kill-switch", data={"next": "/working"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/working"
    assert "Resume new jobs" in client.get("/working").text

    r = client.post("/kill-switch", data={"next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/entry"  # protocol-relative URLs are not followed


def test_working_and_finished_pages_render(client):
    client.post("/entry", data=_form(fiverr_order_no="FO-WEB-2"))
    working = client.get("/working")
    assert working.status_code == 200
    assert "J-0001" in working.text
    assert 'http-equiv="refresh" content="30"' in working.text
    assert client.get("/finished").status_code == 200


def test_run_button_advances_jobs_and_reports_what_happened(client):
    client.post("/entry", data=_form(fiverr_order_no="FO-WEB-4"))
    r = client.post("/run", follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    # Intake passes with the fake responder; the build placeholder then fails the job.
    assert "builder not configured" in page.text
    assert client.get("/jobs/J-0001").text.count("failed") >= 1


def test_run_button_respects_the_kill_switch(client):
    client.post("/kill-switch", data={"next": "/working"})
    r = client.post("/run", follow_redirects=False)
    assert "Not+run" in r.headers["location"] or "Not%20run" in r.headers["location"]
    assert "kill switch is on" in client.get(r.headers["location"]).text


def test_bad_job_ids_are_not_found(client):
    assert client.get("/jobs/not-a-job").status_code == 404
    assert client.get("/jobs/J-9999").status_code == 404


def test_signoff_error_is_shown_as_a_400_page(client):
    client.post("/entry", data=_form(fiverr_order_no="FO-WEB-3"))
    r = client.post("/jobs/J-0001/approve")
    assert r.status_code == 400
    assert "only jobs in review can be approved" in r.text


def test_manual_finance_entry_and_csv_import(client):
    r = client.post("/finance/manual", data={
        "entry_date": "2026-10-03", "kind": "in", "category": "client",
        "description": "cash job", "amount_aud": "80",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/finance?month=2026-10"
    assert "80.00" in client.get("/finance?month=2026-10").text

    csv = b"Date,Description,Amount,Transaction ID\n2026-10-04,Logo,25.00,TX-9\n"
    r = client.post("/finance/import", files={"csv_file": ("export.csv", csv, "text/csv")},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/finance?month=2026-10&added=1&duplicates=0&rejected=0")
    assert "1 added" in page.text
