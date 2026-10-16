"""Append-only JSON-lines event log at <jobs root>/events.log.

Written after a DB transaction commits, so the log never records a change the
database rolled back. Do not pass secrets in `record`.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import config
from app.db import now_iso


def append(record: dict, jobs_root: Path | None = None) -> None:
    root = jobs_root or config.jobs_root()
    root.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": now_iso(), **record}, sort_keys=True)
    with (root / "events.log").open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
