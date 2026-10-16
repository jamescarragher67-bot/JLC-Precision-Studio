"""FastAPI dashboard. Bound to 127.0.0.1 only.

Run it from the job_desk folder:  python -m app.main
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import config, db, finance, jobs, signoff
from app.jobs import JobDeskError, JobInputError
from app.orchestrator import run_once
from app.state import Status
from app.stages.fusion_bridge import export_path

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

ACTIVE_STATUSES = (
    Status.INTAKE, Status.TICKET_READY, Status.BUILDING, Status.CHECKING,
    Status.REVIEW, Status.APPROVED, Status.NEEDS_INPUT, Status.FAILED,
)
FINISHED_STATUSES = (Status.DELIVERED, Status.PAID)
MAX_CSV_BYTES = 2 * 1024 * 1024

app = FastAPI(title="Job desk", docs_url=None, redoc_url=None, openapi_url=None)
# Reject requests whose Host header is not loopback (DNS rebinding guard).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def _header(conn) -> dict[str, Any]:
    spend = db.spend_for_month(conn, db.current_month())
    cap = db.monthly_cap(conn)
    pct = 0.0 if cap <= 0 else min(100.0, spend / cap * 100)
    return {
        "month": db.current_month(),
        "spend": spend,
        "cap": cap,
        "spend_pct": round(pct, 1),
        "kill_switch": db.kill_switch_on(conn),
        "fiverr_url": config.FIVERR_URL,
    }


def _render(request: Request, name: str, conn, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, {**_header(conn), **ctx}, status_code=status_code
    )


def _job_id_or_404(job_id: str) -> str:
    if not jobs.JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404)
    return job_id


def _safe_next(raw: str) -> str:
    """Only allow same-site relative redirects."""
    return raw if raw.startswith("/") and not raw.startswith("//") else "/entry"


def _latest_report(folder: Path) -> str | None:
    reports = sorted((folder / "package").glob("pkg_*/report.md"))
    return reports[-1].read_text(encoding="utf-8") if reports else None


@app.exception_handler(JobDeskError)
async def _operator_error(request: Request, exc: JobDeskError):
    with db.connection() as conn:
        return _render(request, "error.html", conn, status_code=400, message=str(exc))


# --- job entry ---------------------------------------------------------------

@app.get("/")
def root():
    return RedirectResponse("/entry", status_code=303)


@app.get("/entry", response_class=HTMLResponse)
def entry_page(request: Request):
    with db.connection() as conn:
        return _render(request, "entry.html", conn, active="entry")


@app.post("/entry")
async def create_job_route(
    client: str = Form(""),
    fiverr_order_no: str = Form(""),
    order_text: str = Form(""),
    length_mm: str = Form(""),
    width_mm: str = Form(""),
    hole_diameter_mm: str = Form(""),
    price: str = Form(""),
    deadline: str = Form(""),
    sketch: UploadFile | None = File(None),
):
    sketch_name = None
    sketch_bytes = None
    if sketch is not None and sketch.filename:
        sketch_name = sketch.filename
        sketch_bytes = await sketch.read(config.MAX_SKETCH_BYTES + 1)
    new_job = jobs.make_new_job(
        client=client,
        fiverr_order_no=fiverr_order_no,
        order_text=order_text,
        length_mm=length_mm,
        width_mm=width_mm,
        hole_diameter_mm=hole_diameter_mm,
        price=price,
        deadline=deadline,
        sketch_name=sketch_name,
        sketch_bytes=sketch_bytes,
    )
    with db.connection() as conn:
        try:
            job_id = jobs.create_job(conn, new_job, config.jobs_root())
        except Exception as exc:  # e.g. a duplicate Fiverr order number
            if "fiverr_order_no" in str(exc):
                raise JobInputError("a job with this Fiverr order number already exists") from exc
            raise
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# --- working and finished ----------------------------------------------------

@app.get("/working", response_class=HTMLResponse)
def working_page(request: Request, note: str | None = None):
    with db.connection() as conn:
        rows = db.list_jobs(conn, [s.value for s in ACTIVE_STATUSES])
        counts = {s.value: 0 for s in ACTIVE_STATUSES}
        for r in rows:
            counts[r["status"]] += 1
        return _render(request, "working.html", conn, active="working",
                       jobs=rows, counts=counts, statuses=ACTIVE_STATUSES,
                       note=(note or "")[:300])


@app.post("/run")
def run_pipeline_now():
    """Run the orchestrator once. There is no scheduler yet; this button is the trigger."""
    with db.connection() as conn:
        summary = run_once(conn, jobs_root=config.jobs_root())
    if summary.skipped_reason:
        text = f"Not run: {summary.skipped_reason}."
    elif summary.steps:
        text = "; ".join(summary.steps[:5])
    else:
        text = "Nothing to do."
    return RedirectResponse("/working?" + urlencode({"note": text}), status_code=303)


@app.get("/finished", response_class=HTMLResponse)
def finished_page(request: Request):
    with db.connection() as conn:
        rows = [
            {
                "job": r,
                "token_cost": db.job_token_cost(conn, r["id"]),
            }
            for r in db.list_jobs(conn, [s.value for s in FINISHED_STATUSES])
        ]
        return _render(request, "finished.html", conn, active="finished", rows=rows)


# --- finance -----------------------------------------------------------------

def _money(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise finance.FinanceInputError("amount must be a number") from exc


@app.get("/finance", response_class=HTMLResponse)
def finance_page(request: Request, month: str | None = None,
                 added: int | None = None, duplicates: int | None = None,
                 rejected: int | None = None):
    month = finance.valid_month(month)
    with db.connection() as conn:
        return _render(
            request, "finance.html", conn,
            active="finance",
            month=month,
            totals=finance.totals(conn, month),
            entries=finance.list_entries(conn, month),
            api_spend_usd=db.spend_for_month(conn, month),
            mapping=config.FIVERR_CSV_MAPPING,
            import_result={"added": added, "duplicates": duplicates, "rejected": rejected}
            if added is not None else None,
        )


@app.post("/finance/manual")
def finance_manual(
    entry_date: str = Form(...),
    kind: str = Form(...),
    category: str = Form(""),
    description: str = Form(""),
    amount_aud: str = Form(...),
):
    with db.connection() as conn:
        with db.transaction(conn):
            finance.add_entry(
                conn,
                entry_date=entry_date,
                kind=kind,
                category=category,
                description=description,
                amount_aud=_money(amount_aud),
            )
    return RedirectResponse(f"/finance?month={entry_date[:7]}", status_code=303)


@app.post("/finance/import")
async def finance_import(csv_file: UploadFile = File(...)):
    raw = await csv_file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise finance.FinanceInputError("CSV is larger than 2 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise finance.FinanceInputError("CSV must be UTF-8 encoded") from exc
    with db.connection() as conn:
        with db.transaction(conn):
            result = finance.import_fiverr_csv(conn, text)
    month = db.current_month()
    return RedirectResponse(
        f"/finance?month={month}&added={result.added}"
        f"&duplicates={result.duplicates}&rejected={len(result.rejected)}",
        status_code=303,
    )


# --- job detail and sign-off -------------------------------------------------

@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        job = db.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404)
        folder = jobs.job_folder(config.jobs_root(), job_id)
        checks = db.latest_checks(conn, job_id)
        return _render(
            request, "job_detail.html", conn,
            active="working",
            job=job,
            status=Status(job["status"]),
            stages=db.list_stages(conn, job_id),
            checks=checks,
            can_approve=bool(checks) and all(c["passed"] for c in checks),
            typed=db.get_measurements(conn, job_id, source="typed"),
            report=_latest_report(folder),
            token_cost=db.job_token_cost(conn, job_id),
            fusion_path=str(export_path(folder)),
            folder=str(folder),
        )


@app.post("/jobs/{job_id}/approve")
def approve_job(job_id: str):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        signoff.approve(conn, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/send-back")
def send_back_job(job_id: str, notes: str = Form("")):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        signoff.send_back(conn, job_id, notes)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/pause")
def pause_job(job_id: str, paused: str = Form("1")):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        signoff.set_paused(conn, job_id, paused == "1")
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/resume")
def resume_job(job_id: str):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        signoff.resume(conn, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        signoff.retry_failed(conn, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/deliver")
def deliver_job(job_id: str):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        signoff.deliver(conn, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/paid")
def paid_job(job_id: str):
    _job_id_or_404(job_id)
    with db.connection() as conn:
        signoff.mark_paid(conn, job_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/kill-switch")
def toggle_kill_switch(next_path: str = Form("/entry", alias="next")):
    with db.connection() as conn:
        signoff.set_kill_switch(conn, not db.kill_switch_on(conn))
    return RedirectResponse(_safe_next(next_path), status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
