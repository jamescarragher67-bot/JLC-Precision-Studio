"""The single wrapper for every model call.

call_model() runs these steps in order:
  1. Refuse if the kill switch is on.
  2. Refuse if the call would take month-to-date spend over the cap.
  3. Make the call through a ModelClient.
  4. Record tokens and cost in api_calls.

This session implements only FakeClient, which is used in tests and by the
stubbed stages. There is no real network client here.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app import config, db

ROLES = frozenset({"intake", "build", "spec_review"})


class ApiBlocked(Exception):
    """A model call was refused by the kill switch, the cap, or missing pricing."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    input_tokens: int
    output_tokens: int


class ModelClient(Protocol):
    def complete(self, *, model: str, prompt: str, max_output_tokens: int) -> ModelReply:
        ...


def estimate_tokens(text: str) -> int:
    # Rough estimate: about four characters per token. Only used for the
    # pre-call cap check; recorded costs use the tokens the client reports.
    return max(1, math.ceil(len(text) / 4))


def cost_usd(price: dict, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * price["input_per_mtok"] / 1_000_000
        + output_tokens * price["output_per_mtok"] / 1_000_000
    )


class FakeClient:
    """Test double. Returns responder(prompt) and never touches the network."""

    def __init__(self, responder: Callable[[str], str] | None = None) -> None:
        self.responder = responder or (lambda prompt: "")
        self.calls: list[str] = []  # models called, for test assertions

    def complete(self, *, model: str, prompt: str, max_output_tokens: int) -> ModelReply:
        self.calls.append(model)
        text = self.responder(prompt)
        return ModelReply(
            text=text,
            input_tokens=estimate_tokens(prompt),
            output_tokens=min(estimate_tokens(text), max_output_tokens),
        )


def call_model(
    job_id: str,
    role: str,
    model: str,
    prompt: str,
    *,
    conn: sqlite3.Connection,
    client: ModelClient,
    max_output_tokens: int = 1024,
) -> ModelReply:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")

    # 1. Kill switch.
    if db.kill_switch_on(conn):
        raise ApiBlocked("kill switch is on; no model calls")

    # Refuse unpriced models. Without a rate the cap cannot be enforced.
    price = config.price_for(model)
    if price is None:
        raise ApiBlocked(
            f"no pricing configured for {model}; refusing so the spend cap can be enforced"
        )

    # 2. Cap, checked against the worst case before the call is made.
    spent = db.spend_for_month(conn, db.current_month())
    cap = db.monthly_cap(conn)
    worst_case = cost_usd(price, estimate_tokens(prompt), max_output_tokens)
    if spent + worst_case > cap:
        raise ApiBlocked(
            f"monthly API cap ${cap:.2f} would be exceeded "
            f"(spent ${spent:.2f}, this call up to ${worst_case:.4f})"
        )

    # 3. The call.
    reply = client.complete(model=model, prompt=prompt, max_output_tokens=max_output_tokens)

    # 4. Record.
    cost = cost_usd(price, reply.input_tokens, reply.output_tokens)
    db.record_api_call(
        conn, job_id, role, model, reply.input_tokens, reply.output_tokens, cost
    )
    return reply
