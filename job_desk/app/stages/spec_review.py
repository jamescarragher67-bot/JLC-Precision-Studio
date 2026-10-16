"""Spec review stage: a second model pass over the ticket and the checks.

For now this uses a fake responder that returns a fixed review. The output is
saved to checks/spec_review_NNN.md.

TODO(model): swap the fake responder for the real Haiku client.
"""

from __future__ import annotations

from app import config
from app.api_client import FakeClient, call_model
from app.jobs import next_free_path, write_new
from app.stages import StageContext, StageOutput

FAKE_REVIEW = (
    "Spec review (fake responder). The ticket matches the typed measurements. "
    "No issues flagged. Replace this with a real review once the model is wired in."
)


def _latest_ticket_text(ctx: StageContext) -> str:
    tickets = sorted((ctx.folder / "ticket").glob("ticket_*.json"))
    return tickets[-1].read_text(encoding="utf-8") if tickets else ""


def build_prompt(ctx: StageContext) -> str:
    return "\n".join([
        "Review this ticket for a CAD job. Flag anything that does not match the order.",
        "",
        "order text:",
        ctx.job["order_text"],
        "",
        "ticket:",
        _latest_ticket_text(ctx),
    ])


def run(ctx: StageContext) -> StageOutput:
    client = ctx.clients.get("spec_review") or FakeClient(responder=lambda _p: FAKE_REVIEW)
    reply = call_model(
        ctx.job_id,
        "spec_review",
        config.HAIKU_MODEL,
        build_prompt(ctx),
        conn=ctx.conn,
        client=client,
    )
    if not reply.text.strip():
        # Empty output is a transient fault, so it is retried rather than blocked.
        raise RuntimeError("spec review returned an empty reply")
    out = next_free_path(ctx.folder / "checks", "spec_review", ".md")
    write_new(out, reply.text.rstrip() + "\n")
    return StageOutput(output_path=out)
