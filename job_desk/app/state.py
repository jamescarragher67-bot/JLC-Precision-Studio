"""Status enum and the single table of allowed transitions.

Any status change goes through move(). A (from, to, actor) triple that is not
in TRANSITIONS raises InvalidTransition. Only the orchestrator and the sign-off
actions (app/signoff.py) may change status.
"""

from __future__ import annotations

from enum import StrEnum

from app import db, events


class Status(StrEnum):
    DRAFT = "draft"
    INTAKE = "intake"
    TICKET_READY = "ticket_ready"
    BUILDING = "building"
    CHECKING = "checking"
    REVIEW = "review"
    APPROVED = "approved"
    DELIVERED = "delivered"
    PAID = "paid"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


PIPELINE_ORDER: tuple[Status, ...] = (
    Status.DRAFT,
    Status.INTAKE,
    Status.TICKET_READY,
    Status.BUILDING,
    Status.CHECKING,
    Status.REVIEW,
    Status.APPROVED,
    Status.DELIVERED,
    Status.PAID,
)
SIDE_STATES: tuple[Status, ...] = (Status.NEEDS_INPUT, Status.FAILED)


class Actor(StrEnum):
    ORCHESTRATOR = "orchestrator"
    SIGNOFF = "signoff"


class InvalidTransition(Exception):
    """Raised for any (from, to, actor) not listed in TRANSITIONS."""


_ORCH = frozenset({Actor.ORCHESTRATOR})
_SIGN = frozenset({Actor.SIGNOFF})
_S = Status

# The one table. Keys are (from, to); values are the actors allowed to make it.
TRANSITIONS: dict[tuple[Status, Status], frozenset[Actor]] = {
    # Orchestrator: pipeline progress and stage outcomes.
    (_S.INTAKE, _S.TICKET_READY): _ORCH,
    (_S.TICKET_READY, _S.BUILDING): _ORCH,
    (_S.TICKET_READY, _S.FAILED): _ORCH,  # e.g. "builder not configured"
    (_S.BUILDING, _S.CHECKING): _ORCH,
    (_S.CHECKING, _S.REVIEW): _ORCH,  # packaging only, after check results exist
    (_S.INTAKE, _S.NEEDS_INPUT): _ORCH,
    (_S.TICKET_READY, _S.NEEDS_INPUT): _ORCH,
    (_S.BUILDING, _S.NEEDS_INPUT): _ORCH,
    (_S.CHECKING, _S.NEEDS_INPUT): _ORCH,
    # Sign-off: human decisions. The orchestrator can never approve.
    (_S.DRAFT, _S.INTAKE): _SIGN,  # reserved: jobs are created at intake today
    (_S.REVIEW, _S.APPROVED): _SIGN,
    (_S.REVIEW, _S.TICKET_READY): _SIGN,  # send back with notes
    (_S.APPROVED, _S.DELIVERED): _SIGN,
    (_S.DELIVERED, _S.PAID): _SIGN,
    (_S.NEEDS_INPUT, _S.INTAKE): _SIGN,  # resume after the operator fixes input
    (_S.NEEDS_INPUT, _S.TICKET_READY): _SIGN,
    (_S.NEEDS_INPUT, _S.BUILDING): _SIGN,
    (_S.NEEDS_INPUT, _S.CHECKING): _SIGN,
    (_S.FAILED, _S.TICKET_READY): _SIGN,  # retry after the builder is configured
}


def check_transition(current: Status, target: Status, actor: Actor) -> None:
    allowed = TRANSITIONS.get((current, target))
    if allowed is None or actor not in allowed:
        raise InvalidTransition(
            f"{current} -> {target} is not allowed for {actor}"
        )


def allowed_targets(current: Status, actor: Actor) -> list[Status]:
    return [t for (f, t), actors in TRANSITIONS.items() if f == current and actor in actors]


def move(
    tx: db.Tx,
    jobs_root,
    job_id: str,
    target: Status,
    actor: Actor,
    reason: str = "",
) -> Status:
    """Change a job's status inside the caller's transaction.

    Resets stage_cursor and retry_count, because both are per status group.
    The event log line is written only after the transaction commits.
    Returns the previous status.
    """
    row = db.get_job(tx.conn, job_id)
    if row is None:
        raise KeyError(f"no such job: {job_id}")
    current = Status(row["status"])
    check_transition(current, target, actor)
    now = db.now_iso()
    db.update_job(
        tx.conn,
        job_id,
        status=target.value,
        updated_at=now,
        stage_cursor=None,
        retry_count=0,
    )
    record = {
        "event": "status_changed",
        "job_id": job_id,
        "from": current.value,
        "to": target.value,
        "actor": actor.value,
        "reason": reason,
    }
    tx.on_commit(lambda: events.append(record, jobs_root))
    return current
