"""Transition table: allowed and disallowed transitions, and the actors allowed on each."""

import json

import pytest

from app import db, state
from app.state import Actor, InvalidTransition, Status, check_transition, move


def test_pipeline_order_matches_spec():
    assert [s.value for s in state.PIPELINE_ORDER] == [
        "draft", "intake", "ticket_ready", "building", "checking",
        "review", "approved", "delivered", "paid",
    ]
    assert {s.value for s in state.SIDE_STATES} == {"needs_input", "failed"}


@pytest.mark.parametrize("current,target,actor", [
    (Status.INTAKE, Status.TICKET_READY, Actor.ORCHESTRATOR),
    (Status.TICKET_READY, Status.FAILED, Actor.ORCHESTRATOR),
    (Status.CHECKING, Status.REVIEW, Actor.ORCHESTRATOR),
    (Status.REVIEW, Status.APPROVED, Actor.SIGNOFF),
    (Status.REVIEW, Status.TICKET_READY, Actor.SIGNOFF),
    (Status.APPROVED, Status.DELIVERED, Actor.SIGNOFF),
    (Status.DELIVERED, Status.PAID, Actor.SIGNOFF),
    (Status.NEEDS_INPUT, Status.CHECKING, Actor.SIGNOFF),
    (Status.FAILED, Status.TICKET_READY, Actor.SIGNOFF),
])
def test_allowed_transitions(current, target, actor):
    check_transition(current, target, actor)  # no exception


@pytest.mark.parametrize("current,target,actor", [
    # Not in the table at all.
    (Status.INTAKE, Status.PAID, Actor.ORCHESTRATOR),
    (Status.INTAKE, Status.REVIEW, Actor.ORCHESTRATOR),
    (Status.REVIEW, Status.REVIEW, Actor.SIGNOFF),
    (Status.PAID, Status.INTAKE, Actor.SIGNOFF),
    # In the table, but the wrong actor.
    (Status.REVIEW, Status.APPROVED, Actor.ORCHESTRATOR),
    (Status.CHECKING, Status.REVIEW, Actor.SIGNOFF),
    (Status.NEEDS_INPUT, Status.CHECKING, Actor.ORCHESTRATOR),
])
def test_disallowed_transitions_raise(current, target, actor):
    with pytest.raises(InvalidTransition):
        check_transition(current, target, actor)


def test_orchestrator_can_never_approve():
    orchestrator_targets = state.allowed_targets(Status.REVIEW, Actor.ORCHESTRATOR)
    assert Status.APPROVED not in orchestrator_targets
    for (frm, to), actors in state.TRANSITIONS.items():
        if to == Status.APPROVED:
            assert actors == {Actor.SIGNOFF}


def test_move_changes_status_resets_counters_and_logs(conn, make_job, home):
    job_id = make_job()
    db.update_job(conn, job_id, retry_count=2, stage_cursor="checks")
    with db.transaction(conn) as tx:
        previous = move(tx, home.jobs, job_id, Status.TICKET_READY, Actor.ORCHESTRATOR, "test")
    assert previous == Status.INTAKE
    row = db.get_job(conn, job_id)
    assert row["status"] == "ticket_ready"
    assert row["retry_count"] == 0
    assert row["stage_cursor"] is None

    lines = (home.jobs / "events.log").read_text(encoding="utf-8").splitlines()
    changes = [json.loads(x) for x in lines if '"status_changed"' in x]
    assert changes[-1]["from"] == "intake"
    assert changes[-1]["to"] == "ticket_ready"
    assert changes[-1]["job_id"] == job_id


def test_move_refuses_an_invalid_change_and_leaves_status_alone(conn, make_job, home):
    job_id = make_job()
    with pytest.raises(InvalidTransition):
        with db.transaction(conn) as tx:
            move(tx, home.jobs, job_id, Status.PAID, Actor.SIGNOFF)
    assert db.get_job(conn, job_id)["status"] == "intake"
