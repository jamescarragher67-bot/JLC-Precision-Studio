"""Job creation, folder layout, and the never-overwrite file helpers.

Files inside job folders are written with mode "x" or via next_free_path(),
so a second write never replaces an earlier one.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app import config, db, events
from app.state import Status

SUBFOLDERS = ("ticket", "inputs", "model", "checks", "package", "log")
JOB_ID_RE = re.compile(r"^J-\d{4,}$")


class JobDeskError(Exception):
    """Base for errors the dashboard shows to the operator."""


class JobInputError(JobDeskError):
    """Bad input on the job entry form."""


@dataclass(frozen=True)
class NewJob:
    client: str
    fiverr_order_no: str | None
    order_text: str
    length_mm: float
    width_mm: float
    hole_diameter_mm: float
    price: float
    deadline: str  # ISO date, YYYY-MM-DD
    sketch_name: str | None = None
    sketch_bytes: bytes | None = None

    @property
    def title(self) -> str:
        first_line = self.order_text.strip().splitlines()[0]
        return first_line[:80] if first_line else f"Job for {self.client}"


def _positive(label: str, raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise JobInputError(f"{label} must be a number") from exc
    if not value > 0:
        raise JobInputError(f"{label} must be greater than zero")
    return value


def make_new_job(
    *,
    client: str,
    order_text: str,
    length_mm,
    width_mm,
    hole_diameter_mm,
    price,
    deadline: str,
    fiverr_order_no: str | None = None,
    sketch_name: str | None = None,
    sketch_bytes: bytes | None = None,
) -> NewJob:
    client = (client or "").strip()
    order_text = (order_text or "").strip()
    if not client:
        raise JobInputError("client is required")
    if not order_text:
        raise JobInputError("order text is required")
    try:
        date.fromisoformat(deadline)
    except (TypeError, ValueError) as exc:
        raise JobInputError("deadline must be a date (YYYY-MM-DD)") from exc
    try:
        price_value = float(price)
    except (TypeError, ValueError) as exc:
        raise JobInputError("price must be a number") from exc
    if price_value < 0:
        raise JobInputError("price cannot be negative")

    if sketch_bytes is not None:
        if len(sketch_bytes) > config.MAX_SKETCH_BYTES:
            raise JobInputError("sketch is larger than the 10 MB limit")
        ext = Path(sketch_name or "").suffix.lower()
        if ext not in config.SKETCH_EXTENSIONS:
            raise JobInputError(f"sketch type {ext or '(none)'} is not allowed")

    order_no = (fiverr_order_no or "").strip() or None
    return NewJob(
        client=client,
        fiverr_order_no=order_no,
        order_text=order_text,
        length_mm=_positive("length", length_mm),
        width_mm=_positive("width", width_mm),
        hole_diameter_mm=_positive("hole diameter", hole_diameter_mm),
        price=price_value,
        deadline=deadline,
        sketch_name=sketch_name,
        sketch_bytes=sketch_bytes,
    )


def job_folder(jobs_root: Path, job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id):
        raise JobInputError(f"bad job id: {job_id!r}")
    return jobs_root / job_id


# --- never-overwrite file helpers -----------------------------------------

def write_new(path: Path, data: str | bytes) -> Path:
    """Create a file. Raises FileExistsError rather than replacing one."""
    if isinstance(data, bytes):
        with path.open("xb") as f:
            f.write(data)
    else:
        with path.open("x", encoding="utf-8", newline="\n") as f:
            f.write(data)
    return path


def copy_new(src: Path, dst: Path) -> Path:
    with src.open("rb") as r, dst.open("xb") as w:
        shutil.copyfileobj(r, w)
    return dst


def next_free_path(folder: Path, stem: str, suffix: str) -> Path:
    """First of folder/stem_001suffix, stem_002suffix, ... that does not exist."""
    n = 1
    while True:
        candidate = folder / f"{stem}_{n:03d}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def next_numbered_dir(parent: Path, prefix: str) -> Path:
    n = 1
    while True:
        candidate = parent / f"{prefix}_{n:03d}"
        if not candidate.exists():
            candidate.mkdir(parents=True)
            return candidate
        n += 1


def _safe_sketch_name(name: str) -> str:
    # The extension was already checked against SKETCH_EXTENSIONS.
    path = Path(name)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem).strip("_")[:80] or "sketch"
    return f"{stem}{path.suffix.lower()}"


# --- creation --------------------------------------------------------------

def create_job(
    conn: sqlite3.Connection, job: NewJob, jobs_root: Path | None = None
) -> str:
    """Create the job row, typed measurements, and folder tree. Returns the job id.

    Starts at status intake. The status is set here directly because creating
    a job is not a status change; the transition table covers later changes.
    """
    root = jobs_root or config.jobs_root()
    now = db.now_iso()
    with db.transaction(conn) as tx:
        job_id = db.next_job_id(conn)
        folder = job_folder(root, job_id)
        folder.mkdir(parents=True, exist_ok=False)
        try:
            for sub in SUBFOLDERS:
                (folder / sub).mkdir()
            write_new(folder / "ticket" / "order.txt", job.order_text + "\n")

            # NewJob attribute names match the config.TYPED_MEASUREMENTS keys.
            typed_rows = [
                {
                    "name": name,
                    "expected": getattr(job, field),
                    "tolerance": config.TYPED_TOLERANCE_MM,
                    "unit": unit,
                    "source": "typed",
                }
                for field, (name, unit) in config.TYPED_MEASUREMENTS.items()
            ]
            write_new(
                folder / "ticket" / "measurements.json",
                json.dumps(typed_rows, indent=2) + "\n",
            )

            if job.sketch_bytes is not None and job.sketch_name:
                # inputs/ is empty in a fresh folder, so the name cannot collide.
                write_new(
                    folder / "inputs" / _safe_sketch_name(job.sketch_name),
                    job.sketch_bytes,
                )

            db.insert_job(
                conn,
                id=job_id,
                title=job.title,
                client=job.client,
                fiverr_order_no=job.fiverr_order_no,
                order_text=job.order_text,
                price=job.price,
                deadline=job.deadline,
                status=Status.INTAKE.value,
                created_at=now,
                updated_at=now,
            )
            for row in typed_rows:
                db.insert_measurement(
                    conn,
                    job_id,
                    row["name"],
                    row["expected"],
                    row["tolerance"],
                    row["unit"],
                    row["source"],
                )
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        record = {
            "event": "job_created",
            "job_id": job_id,
            "to": Status.INTAKE.value,
        }
        tx.on_commit(lambda: events.append(record, root))
    return job_id
