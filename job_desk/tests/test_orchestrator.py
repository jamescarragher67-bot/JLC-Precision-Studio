"""Orchestrator behaviour: stage order, blocks, retries, kill switch, cap, and sign-off.

Most tests use fake stages. The build stage is replaced by a fake that writes a
Fusion-style export. Intake and spec review use their fake responders.
"""

import json
from dataclasses import replace

import pytest

from app import config, db
from app.api_client import FakeClient
from app.jobs import write_new
from app.orchestrator import PIPELINE_STAGES, run_once
from app.signoff import SignoffError, approve, deliver, mark_paid, resume, send_back, set_kill_switch, set_paused
from app.stages import StageBlocked, StageContext, StageFailed, StageOutput
from app.stages import build as build_stage
from app.stages import packaging
from tests.helpers import GOOD_MEASUREMENTS, write_export


def fake_build(ctx):
    """Stand-in for the builder: writes the measured values the Fusion step would produce."""
    out = ctx.folder / "model" / "measurements.json"
    if not out.exists():
        write_new(out, json.dumps(GOOD_MEASUREMENTS))
    return StageOutput(output_path=out, note="fake build")


def noop_build(ctx):
    return StageOutput(note="fake build, no export")


def stages_with(build_fn):
    return tuple(replace(s, run=build_fn) if s.name == "build" else s
                 for s in PIPELINE_STAGES)


def events(home):
    path = home.jobs / "events.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def status(conn, job_id):
    return db.get_job(conn, job_id)["status"]


def test_builder_placeholder_fails_the_job_and_makes_no_build_call(conn, make_job, home):
    assert config.BUILDER_MODEL is None
    job_id = make_job()
    run_once(conn, jobs_root=home.jobs)

    row = db.get_job(conn, job_id)
    assert row["status"] == "failed"
    build = [s for s in db.list_stages(conn, job_id) if s["stage_name"] == "build"]
    assert len(build) == 1
    assert build[0]["result"] == "failed"
    assert build[0]["error"] == "builder not configured"

    roles = [r["role"] for r in conn.execute(
        "SELECT role FROM api_calls WHERE job_id = ?", (job_id,))]
    assert roles == ["intake"]  # no build call was made
    log = (home.jobs / job_id / "log" / "run.log").read_text(encoding="utf-8")
    assert "builder not configured" in log


def test_build_stage_refuses_when_unconfigured():
    with pytest.raises(StageFailed, match="builder not configured"):
        build_stage.run(None)


def test_configured_but_unimplemented_build_still_fails_safely(monkeypatch):
    monkeypatch.setattr(config, "BUILDER_MODEL", "astra-test")
    with pytest.raises(StageFailed, match="not implemented"):
        build_stage.run(None)


def test_full_run_with_fake_build_reaches_review_and_never_approved(conn, make_job, home):
    job_id = make_job()
    run_once(conn, stages=stages_with(fake_build), jobs_root=home.jobs)

    assert status(conn, job_id) == "review"
    names = [s["stage_name"] for s in db.list_stages(conn, job_id)]
    assert names == ["intake", "build", "fusion_bridge", "checks", "spec_review", "packaging"]
    assert all(s["result"] == "ok" for s in db.list_stages(conn, job_id))

    checks = db.latest_checks(conn, job_id)
    assert len(checks) == 7 and all(c["passed"] for c in checks)
    report = home.jobs / job_id / "package" / "pkg_001" / "report.md"
    assert "7 of 7 checks passed" in report.read_text(encoding="utf-8")

    # Running again changes nothing: review is not actionable for the orchestrator.
    run_once(conn, stages=stages_with(fake_build), jobs_root=home.jobs)
    assert status(conn, job_id) == "review"
    assert len(db.list_stages(conn, job_id)) == 6

    assert all(e.get("to") != "approved" for e in events(home))


def test_only_sign_off_moves_review_to_approved(conn, make_job, home):
    job_id = make_job()
    run_once(conn, stages=stages_with(fake_build), jobs_root=home.jobs)
    approve(conn, job_id, home.jobs)
    assert status(conn, job_id) == "approved"
    deliver(conn, job_id, home.jobs)
    mark_paid(conn, job_id, home.jobs)
    assert status(conn, job_id) == "paid"

    changes = [e for e in events(home) if e.get("event") == "status_changed"]
    to_approved = [e for e in changes if e["to"] == "approved"]
    assert to_approved and all(e["actor"] == "signoff" for e in to_approved)


def test_failing_check_blocks_approval_but_allows_send_back(conn, make_job, home):
    job_id = make_job()
    bad = {**GOOD_MEASUREMENTS, "length": 130.0}
    run_once(conn, stages=stages_with(lambda ctx: _write_bad(ctx, bad)), jobs_root=home.jobs)
    assert status(conn, job_id) == "review"

    with pytest.raises(SignoffError, match="1 check"):
        approve(conn, job_id, home.jobs)
    assert status(conn, job_id) == "review"

    with pytest.raises(SignoffError, match="notes are required"):
        send_back(conn, job_id, "   ", home.jobs)

    send_back(conn, job_id, "Length is 10 mm long. Please revise.", home.jobs)
    row = db.get_job(conn, job_id)
    assert row["status"] == "ticket_ready"
    assert row["revision_count"] == 1
    note = home.jobs / job_id / "ticket" / "revision_001.txt"
    assert "Please revise" in note.read_text(encoding="utf-8")


def _write_bad(ctx, values):
    out = ctx.folder / "model" / "measurements.json"
    if not out.exists():
        write_new(out, json.dumps(values))
    return StageOutput(output_path=out)


def test_missing_fusion_export_needs_input_and_resume_continues(conn, make_job, home):
    job_id = make_job()
    run_once(conn, stages=stages_with(noop_build), jobs_root=home.jobs)
    assert status(conn, job_id) == "needs_input"
    last = db.last_stage(conn, job_id)
    assert last["stage_name"] == "fusion_bridge"
    assert "No Fusion export" in last["error"]

    write_export(home.jobs, job_id)
    resume(conn, job_id, home.jobs)
    assert status(conn, job_id) == "building"

    run_once(conn, stages=stages_with(noop_build), jobs_root=home.jobs)
    assert status(conn, job_id) == "review"


def test_intake_ticket_that_changes_a_typed_number_needs_input(conn, make_job, home):
    job_id = make_job()

    def wrong_length(prompt):
        return json.dumps({
            "job_id": job_id,
            "client": "Acme Fixtures",
            "measurements": [
                {"name": "length", "value": 121.0, "unit": "mm"},
                {"name": "width", "value": 40.0, "unit": "mm"},
                {"name": "hole_diameter", "value": 5.5, "unit": "mm"},
            ],
        })

    run_once(conn, clients={"intake": FakeClient(responder=wrong_length)}, jobs_root=home.jobs)
    assert status(conn, job_id) == "needs_input"
    last = db.last_stage(conn, job_id)
    assert last["stage_name"] == "intake" and last["result"] == "failed"
    assert "changed typed length" in last["error"]


def test_unexpected_errors_retry_twice_then_need_input(conn, make_job, home):
    job_id = make_job()

    def flaky(prompt):
        raise RuntimeError("upstream timeout")

    clients = {"intake": FakeClient(responder=flaky)}
    run_once(conn, clients=clients, jobs_root=home.jobs)
    run_once(conn, clients=clients, jobs_root=home.jobs)
    row = db.get_job(conn, job_id)
    assert row["status"] == "intake"
    assert row["retry_count"] == 2

    run_once(conn, clients=clients, jobs_root=home.jobs)
    row = db.get_job(conn, job_id)
    assert row["status"] == "needs_input"
    stages = [s for s in db.list_stages(conn, job_id) if s["stage_name"] == "intake"]
    assert len(stages) == 3 and all(s["result"] == "failed" for s in stages)
    assert "retry cap reached" in db.last_stage(conn, job_id)["error"]


def test_kill_switch_stops_the_run_and_turning_it_off_resumes(conn, make_job, home):
    job_id = make_job()
    set_kill_switch(conn, True, home.jobs)
    summary = run_once(conn, jobs_root=home.jobs)
    assert summary.skipped_reason == "kill switch is on"
    assert status(conn, job_id) == "intake"
    assert db.list_stages(conn, job_id) == []

    set_kill_switch(conn, False, home.jobs)
    run_once(conn, jobs_root=home.jobs)
    assert status(conn, job_id) == "failed"  # builder placeholder, as expected


def test_spend_cap_stops_the_run(conn, make_job, home):
    job_id = make_job()
    db.set_setting(conn, db.CAP_KEY, "0")
    summary = run_once(conn, jobs_root=home.jobs)
    assert "cap reached" in summary.skipped_reason
    assert status(conn, job_id) == "intake"


def test_paused_job_is_not_advanced(conn, make_job, home):
    job_id = make_job()
    set_paused(conn, job_id, True, home.jobs)
    run_once(conn, jobs_root=home.jobs)
    assert status(conn, job_id) == "intake"
    assert db.list_stages(conn, job_id) == []


def test_packaging_refuses_without_check_results(conn, make_job, home):
    job_id = make_job()
    ctx = StageContext(conn=conn, job_id=job_id, folder=home.jobs / job_id,
                       job=db.get_job(conn, job_id))
    with pytest.raises(StageBlocked, match="no check results"):
        packaging.run(ctx)


def test_api_key_never_reaches_database_or_logs(conn, make_job, home, monkeypatch):
    secret = "sk-ant-SECRET-VALUE-do-not-store-123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    job_id = make_job()
    run_once(conn, stages=stages_with(fake_build), jobs_root=home.jobs)
    conn.close()

    haystacks = [(home.data / "job_desk.sqlite3").read_bytes(),
                 (home.jobs / "events.log").read_bytes(),
                 (home.jobs / job_id / "log" / "run.log").read_bytes()]
    assert all(secret.encode() not in h for h in haystacks)
