"""Human sign-off actions. These are the only paths besides the orchestrator that
may change a job's status. Each one goes through state.move(), so the
transition table still applies.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app import config, db, events
from app.jobs import JobDeskError, job_folder, next_free_path, write_new
from app.orchestrator import PIPELINE_STAGES
from app.state import Actor, InvalidTransition, Status, check_transition, move


class SignoffError(JobDeskError):
    """A sign-off action was refused. The message is shown to the operator."""


def _job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = db.get_job(conn, job_id)
    if row is None:
        raise SignoffError(f"no such job: {job_id}")
    return row


def _transition(conn, root: Path, job_id: str, target: Status, reason: str) -> None:
    try:
        with db.transaction(conn) as tx:
            move(tx, root, job_id, target, Actor.SIGNOFF, reason=reason)
    except InvalidTransition as exc:
        raise SignoffError(str(exc)) from exc


def approve(conn: sqlite3.Connection, job_id: str, jobs_root: Path | None = None) -> None:
    root = jobs_root or config.jobs_root()
    job = _job(conn, job_id)
    if job["status"] != Status.REVIEW.value:
        raise SignoffError(f"only jobs in review can be approved (status is {job['status']})")
    results = db.latest_checks(conn, job_id)
    if not results:
        raise SignoffError("no check results on record, so the job cannot be approved")
    failing = [r["check_name"] for r in results if not r["passed"]]
    if failing:
        raise SignoffError(
            f"{len(failing)} check(s) failed ({', '.join(failing)}). Send the job back instead."
        )
    _transition(conn, root, job_id, Status.APPROVED, "approved by operator")


def send_back(
    conn: sqlite3.Connection, job_id: str, notes: str, jobs_root: Path | None = None
) -> None:
    root = jobs_root or config.jobs_root()
    notes = (notes or "").strip()
    if not notes:
        raise SignoffError("notes are required when sending a job back")
    job = _job(conn, job_id)
    try:
        check_transition(Status(job["status"]), Status.TICKET_READY, Actor.SIGNOFF)
    except InvalidTransition as exc:
        raise SignoffError(str(exc)) from exc

    revision = job["revision_count"] + 1
    note_file = next_free_path(job_folder(root, job_id) / "ticket", "revision", ".txt")
    write_new(note_file, notes + "\n")
    with db.transaction(conn) as tx:
        db.update_job(conn, job_id, revision_count=revision)
        move(tx, root, job_id, Status.TICKET_READY, Actor.SIGNOFF,
             reason=f"sent back, revision {revision}")


def deliver(conn: sqlite3.Connection, job_id: str, jobs_root: Path | None = None) -> None:
    _transition(conn, jobs_root or config.jobs_root(), job_id,
                Status.DELIVERED, "marked delivered by operator")


def mark_paid(conn: sqlite3.Connection, job_id: str, jobs_root: Path | None = None) -> None:
    _transition(conn, jobs_root or config.jobs_root(), job_id,
                Status.PAID, "marked paid by operator")


def retry_failed(conn: sqlite3.Connection, job_id: str, jobs_root: Path | None = None) -> None:
    job = _job(conn, job_id)
    if job["status"] != Status.FAILED.value:
        raise SignoffError("only failed jobs can be retried")
    _transition(conn, jobs_root or config.jobs_root(), job_id,
                Status.TICKET_READY, "operator retry after fixing the cause")


def resume(conn: sqlite3.Connection, job_id: str, jobs_root: Path | None = None) -> None:
    """Move a needs_input job back to the status of the stage that blocked it."""
    root = jobs_root or config.jobs_root()
    job = _job(conn, job_id)
    if job["status"] != Status.NEEDS_INPUT.value:
        raise SignoffError("only jobs that need input can be resumed")
    last = db.last_stage(conn, job_id)
    if last is None:
        raise SignoffError("no stage history to resume from")
    stage = next((s for s in PIPELINE_STAGES if s.name == last["stage_name"]), None)
    if stage is None:
        raise SignoffError(f"unknown stage {last['stage_name']!r}")
    _transition(conn, root, job_id, stage.from_status,
                f"operator resumed after {stage.name} needed input")


def set_paused(
    conn: sqlite3.Connection, job_id: str, paused: bool, jobs_root: Path | None = None
) -> None:
    root = jobs_root or config.jobs_root()
    _job(conn, job_id)
    with db.transaction(conn) as tx:
        db.update_job(conn, job_id, paused=1 if paused else 0, updated_at=db.now_iso())
        record = {"event": "job_paused" if paused else "job_resumed", "job_id": job_id}
        tx.on_commit(lambda: events.append(record, root))


def set_kill_switch(
    conn: sqlite3.Connection, on: bool, jobs_root: Path | None = None
) -> None:
    root = jobs_root or config.jobs_root()
    with db.transaction(conn) as tx:
        db.set_setting(conn, db.KILL_SWITCH_KEY, "1" if on else "0")
        record = {"event": "kill_switch", "on": on}
        tx.on_commit(lambda: events.append(record, root))
