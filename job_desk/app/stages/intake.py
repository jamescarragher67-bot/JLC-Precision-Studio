"""Intake stage: turn the order into a validated ticket through the model wrapper.

For now the model is a fake responder that builds the ticket from the typed
measurements. The validation is real and runs on whatever the model returns.

TODO(model): swap the fake responder for the real Haiku client. Only api_client
should make model calls.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from app import config, db
from app.api_client import FakeClient, call_model
from app.jobs import next_free_path, write_new
from app.stages import StageBlocked, StageContext, StageOutput


def build_prompt(job: sqlite3.Row, typed: Sequence[sqlite3.Row]) -> str:
    lines = [
        "You are the intake assistant for a parametric CAD job desk.",
        "Turn the order below into a ticket. Reply with JSON only, with keys:",
        "job_id, client, measurements (list of {name, value, unit}), notes.",
        "Copy every typed measurement exactly. Do not change any number.",
        "",
        f"job_id: {job['id']}",
        f"client: {job['client']}",
        "order text:",
        job["order_text"],
        "typed measurements:",
    ]
    for row in typed:
        lines.append(f"- {row['name']}: {row['expected']} {row['unit']}")
    return "\n".join(lines)


def fake_ticket_responder(
    job_id: str, client: str, typed: Sequence[sqlite3.Row]
) -> Callable[[str], str]:
    """Fake model: returns a ticket built from the typed measurements."""

    def respond(_prompt: str) -> str:
        return json.dumps(
            {
                "job_id": job_id,
                "client": client,
                "measurements": [
                    {"name": r["name"], "value": r["expected"], "unit": r["unit"]}
                    for r in typed
                ],
                "notes": "Built by the fake intake responder from typed measurements.",
            }
        )

    return respond


def validate_ticket(
    text: str, job_id: str, typed: Sequence[sqlite3.Row]
) -> dict[str, Any]:
    """Check the ticket's shape and that every typed number is unchanged.

    Raises StageBlocked on any problem, which sends the job to needs_input.
    """
    try:
        ticket = json.loads(text)
    except ValueError as exc:
        raise StageBlocked(f"intake ticket is not valid JSON: {exc}") from exc
    if not isinstance(ticket, dict):
        raise StageBlocked("intake ticket must be a JSON object")
    if ticket.get("job_id") != job_id:
        raise StageBlocked(f"intake ticket job_id {ticket.get('job_id')!r} is not {job_id}")
    if not isinstance(ticket.get("client"), str):
        raise StageBlocked("intake ticket is missing a client name")
    measurements = ticket.get("measurements")
    if not isinstance(measurements, list):
        raise StageBlocked("intake ticket measurements must be a list")

    by_name: dict[str, dict] = {}
    for m in measurements:
        if not isinstance(m, dict) or not isinstance(m.get("name"), str):
            raise StageBlocked("intake ticket has a measurement without a name")
        if not isinstance(m.get("unit"), str):
            raise StageBlocked(f"intake ticket measurement {m['name']} has no unit")
        value = m.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StageBlocked(f"intake ticket measurement {m['name']} is not a number")
        if m["name"] in by_name:
            raise StageBlocked(f"intake ticket repeats measurement {m['name']}")
        by_name[m["name"]] = m

    for row in typed:
        found = by_name.get(row["name"])
        if found is None:
            raise StageBlocked(f"intake ticket is missing typed measurement {row['name']}")
        if float(found["value"]) != float(row["expected"]):
            raise StageBlocked(
                f"intake ticket changed typed {row['name']}: "
                f"typed {row['expected']}, ticket {found['value']}"
            )
        if found["unit"] != row["unit"]:
            raise StageBlocked(f"intake ticket changed the unit for {row['name']}")
    return ticket


def run(ctx: StageContext) -> StageOutput:
    typed = db.get_measurements(ctx.conn, ctx.job_id, source="typed")
    prompt = build_prompt(ctx.job, typed)

    client = ctx.clients.get("intake")
    if client is None:
        client = FakeClient(
            responder=fake_ticket_responder(ctx.job_id, ctx.job["client"], typed)
        )
    reply = call_model(
        ctx.job_id,
        "intake",
        config.HAIKU_MODEL,
        prompt,
        conn=ctx.conn,
        client=client,
    )

    ticket = validate_ticket(reply.text, ctx.job_id, typed)

    # Store measurements that only the ticket provides. Typed ones are already
    # stored at job creation and are never overwritten.
    typed_names = {r["name"] for r in typed}
    known_ticket = {
        r["name"] for r in db.get_measurements(ctx.conn, ctx.job_id, source="ticket")
    }
    for m in ticket["measurements"]:
        if m["name"] in typed_names or m["name"] in known_ticket:
            continue
        db.insert_measurement(
            ctx.conn, ctx.job_id, m["name"], float(m["value"]), None, m["unit"], "ticket"
        )

    out = next_free_path(ctx.folder / "ticket", "ticket", ".json")
    write_new(out, json.dumps(ticket, indent=2) + "\n")
    return StageOutput(output_path=out)
