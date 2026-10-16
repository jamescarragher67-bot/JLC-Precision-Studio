"""Packaging stage: copy the model and check outputs into package/ and write report.md.

Each run writes a new package/pkg_NNN folder, so earlier packages are never
overwritten. The orchestrator moves the job to review after this stage succeeds.
Packaging refuses to run without check results on record.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from app import db
from app.jobs import copy_new, next_numbered_dir, write_new
from app.stages import StageBlocked, StageContext, StageOutput


def _copy_files(src_dir: Path, dst_dir: Path) -> int:
    dst_dir.mkdir(parents=True)
    copied = 0
    if not src_dir.exists():
        return copied
    for src in sorted(src_dir.iterdir()):
        if src.is_file():
            copy_new(src, dst_dir / src.name)
            copied += 1
    return copied


def render_report(
    job: sqlite3.Row,
    checks: Sequence[sqlite3.Row],
    spec_review_text: str | None,
    generated_at: str,
) -> str:
    passed = sum(1 for c in checks if c["passed"])
    lines = [
        f"# {job['id']} - {job['title']}",
        "",
        f"- Client: {job['client']}",
        f"- Fiverr order no.: {job['fiverr_order_no'] or '-'}",
        f"- Price: AUD {job['price']:.2f}",
        f"- Deadline: {job['deadline']}",
        f"- Revision: {job['revision_count'] + 1}",
        f"- Generated: {generated_at}",
        "",
        "## Checks",
        "",
        f"{passed} of {len(checks)} checks passed.",
        "",
        "| Check | Measured | Expected | Tolerance | Result |",
        "|---|---|---|---|---|",
    ]
    for c in checks:
        measured = "missing" if c["measured"] is None else f"{c['measured']:g}"
        lines.append(
            f"| {c['check_name']} | {measured} | {c['expected']:g} "
            f"| +/- {c['tolerance']:g} | {'PASS' if c['passed'] else 'FAIL'} |"
        )
    lines += ["", "## Detail", ""]
    lines += [f"- {c['detail']}" for c in checks]
    if spec_review_text:
        lines += ["", "## Spec review", "", spec_review_text.rstrip()]
    return "\n".join(lines) + "\n"


def run(ctx: StageContext) -> StageOutput:
    checks = db.latest_checks(ctx.conn, ctx.job_id)
    if not checks:
        raise StageBlocked("no check results on record; packaging refused")

    reviews = sorted((ctx.folder / "checks").glob("spec_review_*.md"))
    review_text = reviews[-1].read_text(encoding="utf-8") if reviews else None

    packet = next_numbered_dir(ctx.folder / "package", "pkg")
    _copy_files(ctx.folder / "model", packet / "model")
    _copy_files(ctx.folder / "checks", packet / "checks")

    report = render_report(ctx.job, checks, review_text, db.now_iso())
    out = write_new(packet / "report.md", report)
    return StageOutput(output_path=out)
