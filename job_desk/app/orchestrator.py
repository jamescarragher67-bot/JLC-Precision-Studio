"""run_once(): advance actionable jobs through the pipeline, one stage at a time.

Per stage, run_once does the following:
  - Re-checks the kill switch and the monthly cap before each stage.
  - Runs the stage. The stage returns output or raises one of these:
      ApiBlocked    -> stage recorded as skipped; nothing changes; run stops
      StageBlocked  -> stage recorded as failed; job -> needs_input
      StageFailed   -> stage recorded as failed; job -> failed
      other         -> retried; after MAX_STAGE_RETRIES retries, needs_input
  - On success, records the stage. Then either moves the job to the stage's
    to_status or records the stage in stage_cursor for the next stage.

Only the transitions listed in state.TRANSITIONS are possible, and this module
only ever uses the ORCHESTRATOR actor. The orchestrator cannot approve a job.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app import config, db
from app.api_client import ApiBlocked
from app.jobs import job_folder
from app.state import Actor, Status, move
from app.stages import StageBlocked, StageContext, StageFailed, StageOutput
from app.stages import build, checks, fusion_bridge, intake, packaging, spec_review


@dataclass(frozen=True)
class Stage:
    name: str
    from_status: Status
    to_status: Status | None  # None: the stage leaves the status unchanged
    run: Callable[[StageContext], StageOutput]


PIPELINE_STAGES: tuple[Stage, ...] = (
    Stage("intake", Status.INTAKE, Status.TICKET_READY, intake.run),
    Stage("build", Status.TICKET_READY, Status.BUILDING, build.run),
    Stage("fusion_bridge", Status.BUILDING, Status.CHECKING, fusion_bridge.run),
    Stage("checks", Status.CHECKING, None, checks.run),
    Stage("spec_review", Status.CHECKING, None, spec_review.run),
    Stage("packaging", Status.CHECKING, Status.REVIEW, packaging.run),
)


@dataclass
class RunSummary:
    skipped_reason: str | None = None
    steps: list[str] = field(default_factory=list)


def next_step(job: sqlite3.Row, stages: Sequence[Stage]) -> Stage | None:
    group = [s for s in stages if s.from_status.value == job["status"]]
    if not group:
        return None
    cursor = job["stage_cursor"]
    if cursor is None:
        return group[0]
    names = [s.name for s in group]
    if cursor not in names:
        return group[0]
    idx = names.index(cursor)
    return group[idx + 1] if idx + 1 < len(group) else None


def _guard(conn: sqlite3.Connection) -> str | None:
    """Reason to stop, or None. Checked before every stage."""
    if db.kill_switch_on(conn):
        return "kill switch is on"
    spent = db.spend_for_month(conn, db.current_month())
    cap = db.monthly_cap(conn)
    if spent >= cap:
        return f"monthly API cap reached (spent ${spent:.2f} of ${cap:.2f})"
    return None


def _log(folder: Path, text: str) -> None:
    log_dir = folder / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "run.log").open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{db.now_iso()} {text}\n")


def _record(conn, job_id, step: Stage, started: str, result: str,
            output_path: str | None = None, error: str | None = None) -> None:
    db.record_stage(conn, job_id, step.name, started, db.now_iso(), result,
                    output_path, error)


def _fail(conn, root: Path, job_id: str, step: Stage, started: str,
          error: str, target: Status, summary: RunSummary) -> str:
    with db.transaction(conn) as tx:
        _record(conn, job_id, step, started, "failed", error=error)
        move(tx, root, job_id, target, Actor.ORCHESTRATOR,
             reason=f"{step.name} failed: {error}")
    summary.steps.append(f"{job_id} {step.name} failed -> {target.value}: {error}")
    _log(job_folder(root, job_id), f"{step.name} failed -> {target.value}: {error}")
    return "failed"


def _run_step(conn, root: Path, job: sqlite3.Row, step: Stage,
              clients: Mapping[str, object], summary: RunSummary) -> str:
    job_id = job["id"]
    started = db.now_iso()
    ctx = StageContext(
        conn=conn,
        job_id=job_id,
        folder=job_folder(root, job_id),
        job=job,
        clients=dict(clients),
    )
    try:
        output = step.run(ctx)
    except ApiBlocked as exc:
        _record(conn, job_id, step, started, "skipped", error=str(exc))
        summary.steps.append(f"{job_id} {step.name} skipped: {exc}")
        summary.skipped_reason = str(exc)
        return "skipped"
    except StageBlocked as exc:
        return _fail(conn, root, job_id, step, started, str(exc),
                     Status.NEEDS_INPUT, summary)
    except StageFailed as exc:
        return _fail(conn, root, job_id, step, started, str(exc),
                     Status.FAILED, summary)
    except Exception as exc:  # transient or unexpected: retry, then give up
        error = f"{type(exc).__name__}: {exc}"
        retries = job["retry_count"] + 1
        if retries > config.MAX_STAGE_RETRIES:
            return _fail(conn, root, job_id, step, started,
                         f"retry cap reached after {config.MAX_STAGE_RETRIES} retries: {error}",
                         Status.NEEDS_INPUT, summary)
        with db.transaction(conn):
            _record(conn, job_id, step, started, "failed", error=error)
            db.update_job(conn, job_id, retry_count=retries)
        summary.steps.append(f"{job_id} {step.name} retry {retries}: {error}")
        _log(job_folder(root, job_id), f"{step.name} retry {retries}: {error}")
        return "retry"

    output_path = str(output.output_path) if output.output_path else None
    with db.transaction(conn) as tx:
        _record(conn, job_id, step, started, "ok", output_path=output_path)
        if step.to_status is None:
            db.update_job(conn, job_id, stage_cursor=step.name, retry_count=0)
        else:
            move(tx, root, job_id, step.to_status, Actor.ORCHESTRATOR,
                 reason=f"{step.name} ok")
    summary.steps.append(f"{job_id} {step.name} ok {output.note}".rstrip())
    _log(job_folder(root, job_id), f"{step.name} ok {output.note}".rstrip())
    return "ok"


def run_once(
    conn: sqlite3.Connection,
    *,
    stages: Sequence[Stage] = PIPELINE_STAGES,
    clients: Mapping[str, object] | None = None,
    jobs_root: Path | None = None,
) -> RunSummary:
    root = jobs_root or config.jobs_root()
    summary = RunSummary()

    reason = _guard(conn)
    if reason:
        summary.skipped_reason = reason
        return summary

    statuses = sorted({s.from_status.value for s in stages})
    for row in db.list_jobs(conn, statuses):
        job_id = row["id"]
        for _ in range(config.MAX_STEPS_PER_JOB_PER_PASS):
            job = db.get_job(conn, job_id)
            if job["paused"] or job["status"] not in statuses:
                break
            step = next_step(job, stages)
            if step is None:
                break
            reason = _guard(conn)
            if reason:
                summary.skipped_reason = reason
                return summary
            outcome = _run_step(conn, root, job, step, clients or {}, summary)
            if outcome == "skipped":
                return summary
            if outcome != "ok":
                break  # retry or needs_input/failed: stop this job for this pass
    return summary


if __name__ == "__main__":
    # Manual trigger from the command line: python -m app.orchestrator
    with db.connection() as _conn:
        _summary = run_once(_conn)
    print(_summary.skipped_reason or "\n".join(_summary.steps) or "Nothing to do.")
