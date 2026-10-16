"""The kill switch and the spend cap block model calls. Every model call is recorded."""

import pytest

from app import config, db
from app.api_client import ApiBlocked, FakeClient, call_model, cost_usd
from app.signoff import set_kill_switch


def test_kill_switch_blocks_the_call(conn, make_job):
    job_id = make_job()
    set_kill_switch(conn, True)
    client = FakeClient(responder=lambda p: "{}")
    with pytest.raises(ApiBlocked, match="kill switch"):
        call_model(job_id, "intake", config.HAIKU_MODEL, "hello",
                   conn=conn, client=client)
    assert client.calls == []
    assert conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0


def test_cap_blocks_a_call_that_would_exceed_it(conn, make_job):
    job_id = make_job()
    db.set_setting(conn, db.CAP_KEY, "0")
    client = FakeClient(responder=lambda p: "{}")
    with pytest.raises(ApiBlocked, match="cap"):
        call_model(job_id, "intake", config.HAIKU_MODEL, "hello",
                   conn=conn, client=client)
    assert client.calls == []


def test_cap_counts_spend_already_recorded(conn, make_job):
    job_id = make_job()
    db.set_setting(conn, db.CAP_KEY, "1.00")
    # Pretend this month's spend is almost all of the cap.
    db.record_api_call(conn, job_id, "intake", config.HAIKU_MODEL, 1, 1, 0.999999)
    client = FakeClient(responder=lambda p: "x" * 400)
    with pytest.raises(ApiBlocked):
        call_model(job_id, "intake", config.HAIKU_MODEL, "a longer prompt " * 20,
                   conn=conn, client=client, max_output_tokens=1024)
    assert client.calls == []


def test_successful_call_is_recorded_with_cost(conn, make_job):
    job_id = make_job()
    client = FakeClient(responder=lambda p: "ticket text")
    reply = call_model(job_id, "intake", config.HAIKU_MODEL, "p" * 400,
                       conn=conn, client=client)
    assert client.calls == [config.HAIKU_MODEL]
    row = conn.execute("SELECT * FROM api_calls WHERE job_id = ?", (job_id,)).fetchone()
    assert row["role"] == "intake"
    assert row["model"] == config.HAIKU_MODEL
    assert row["input_tokens"] == reply.input_tokens == 100
    expected = cost_usd(config.PRICING[config.HAIKU_MODEL], reply.input_tokens,
                        reply.output_tokens)
    assert row["cost_usd"] == pytest.approx(expected)


def test_unpriced_model_is_refused(conn, make_job):
    job_id = make_job()
    client = FakeClient(responder=lambda p: "{}")
    with pytest.raises(ApiBlocked, match="no pricing"):
        call_model(job_id, "build", "GPT-6 Astra (not configured)", "hi",
                   conn=conn, client=client)
    assert client.calls == []


def test_unknown_role_is_a_programming_error(conn, make_job):
    job_id = make_job()
    with pytest.raises(ValueError):
        call_model(job_id, "marketing", config.HAIKU_MODEL, "hi",
                   conn=conn, client=FakeClient())


def test_api_keys_are_read_from_env_only(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abc")
    assert config.api_key("ANTHROPIC_API_KEY") == "sk-test-abc"
    with pytest.raises(ValueError):
        config.api_key("SOME_OTHER_KEY")
