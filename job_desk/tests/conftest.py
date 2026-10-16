"""Shared fixtures. Every test gets its own data dir and jobs root under tmp_path,
so nothing touches the real job_desk/data or job_desk/jobs folders.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from app import db
from app.jobs import create_job, make_new_job
from tests.helpers import BRACKET_ARGS


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    jobs_root = tmp_path / "jobs"
    monkeypatch.setenv("JOB_DESK_DATA_DIR", str(data))
    monkeypatch.setenv("JOB_DESK_JOBS_ROOT", str(jobs_root))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return SimpleNamespace(data=data, jobs=jobs_root)


@pytest.fixture
def conn(home):
    with db.connection() as c:
        yield c


@pytest.fixture
def make_job(conn, home):
    order_nos = (f"FO-{n:05d}" for n in itertools.count(1))

    def _make(**overrides):
        args = {"fiverr_order_no": next(order_nos), **BRACKET_ARGS, **overrides}
        return create_job(conn, make_new_job(**args), home.jobs)

    return _make
