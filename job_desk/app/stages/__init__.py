"""Shared types for pipeline stages.

A stage's run(ctx) returns StageOutput on success. It raises one of:
  StageBlocked  -> job goes to needs_input (operator must act; no retry)
  StageFailed   -> job goes to failed (hard stop, e.g. builder not configured)
  any other Exception -> retried, up to MAX_STAGE_RETRIES, then needs_input
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


class StageBlocked(Exception):
    """The stage cannot proceed until an operator fixes something."""


class StageFailed(Exception):
    """The stage failed for a reason retrying will not fix."""


@dataclass
class StageContext:
    conn: sqlite3.Connection
    job_id: str
    folder: Path
    job: sqlite3.Row
    clients: dict = field(default_factory=dict)  # role -> ModelClient override


@dataclass(frozen=True)
class StageOutput:
    output_path: Path | None = None
    note: str = ""
