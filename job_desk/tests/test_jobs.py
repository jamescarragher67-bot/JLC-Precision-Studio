"""Job creation, folder layout, validation, and the never-overwrite rule."""

import json
import sqlite3

import pytest

from app import config, db
from app.jobs import (
    JobInputError,
    make_new_job,
    create_job,
    next_free_path,
    next_numbered_dir,
    write_new,
)
from tests.helpers import BRACKET_ARGS


def test_creates_folders_files_and_intake_status(conn, make_job, home):
    job_id = make_job()
    assert job_id == "J-0001"

    folder = home.jobs / job_id
    for sub in ("ticket", "inputs", "model", "checks", "package", "log"):
        assert (folder / sub).is_dir()
    assert (folder / "ticket" / "order.txt").read_text(encoding="utf-8").strip() == \
        BRACKET_ARGS["order_text"]

    typed = json.loads((folder / "ticket" / "measurements.json").read_text(encoding="utf-8"))
    assert {r["name"]: r["expected"] for r in typed} == {
        "length": 120.0, "width": 40.0, "hole_diameter": 5.5,
    }

    row = db.get_job(conn, job_id)
    assert row["status"] == "intake"
    assert row["title"].startswith("Mounting bracket")
    stored = db.get_measurements(conn, job_id, source="typed")
    assert sorted(r["name"] for r in stored) == ["hole_diameter", "length", "width"]
    assert all(r["tolerance"] == config.TYPED_TOLERANCE_MM for r in stored)


def test_job_ids_increment(make_job):
    assert make_job() == "J-0001"
    assert make_job() == "J-0002"


def test_sketch_is_saved_under_inputs(conn, make_job, home):
    job_id = create_job(conn, make_new_job(
        **{**BRACKET_ARGS, "fiverr_order_no": "FO-SK"},
        sketch_name="../../my sketch.PNG", sketch_bytes=b"\x89PNG fake"),
        home.jobs)
    saved = list((home.jobs / job_id / "inputs").iterdir())
    assert [p.name for p in saved] == ["my_sketch.png"]
    assert saved[0].read_bytes() == b"\x89PNG fake"


def test_write_new_never_overwrites(tmp_path):
    target = tmp_path / "a.txt"
    write_new(target, "first")
    with pytest.raises(FileExistsError):
        write_new(target, "second")
    assert target.read_text(encoding="utf-8") == "first"


def test_next_free_path_and_numbered_dirs_do_not_overwrite(tmp_path):
    first = next_free_path(tmp_path, "run", ".json")
    write_new(first, "{}")
    second = next_free_path(tmp_path, "run", ".json")
    assert first.name == "run_001.json"
    assert second.name == "run_002.json"

    pkg1 = next_numbered_dir(tmp_path, "pkg")
    pkg2 = next_numbered_dir(tmp_path, "pkg")
    assert pkg1.name == "pkg_001" and pkg2.name == "pkg_002"


@pytest.mark.parametrize("overrides,message", [
    ({"client": "  "}, "client"),
    ({"order_text": ""}, "order text"),
    ({"length_mm": 0}, "length"),
    ({"width_mm": "abc"}, "width"),
    ({"hole_diameter_mm": -1}, "hole diameter"),
    ({"price": -5}, "price"),
    ({"deadline": "next tuesday"}, "deadline"),
    ({"sketch_name": "evil.exe", "sketch_bytes": b"MZ"}, "not allowed"),
])
def test_bad_input_is_rejected(overrides, message):
    args = {**BRACKET_ARGS, **overrides}
    with pytest.raises(JobInputError, match=message):
        make_new_job(**args)


def test_duplicate_fiverr_order_is_rejected_and_leaves_no_folder(conn, make_job, home):
    make_job(fiverr_order_no="FO-DUP")
    with pytest.raises(sqlite3.IntegrityError):
        create_job(conn, make_new_job(**{**BRACKET_ARGS, "fiverr_order_no": "FO-DUP"}),
                   home.jobs)
    assert not (home.jobs / "J-0002").exists()
    assert db.get_job(conn, "J-0002") is None
