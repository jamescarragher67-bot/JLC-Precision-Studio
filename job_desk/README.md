# Job desk

A local tracker for parametric CAD jobs. Jobs move through a fixed pipeline.
The AI stages and the Fusion step are stubbed for now. The dashboard listens on
`127.0.0.1` only.

Nothing in this build calls a model or automates Fiverr. The Fiverr link opens
in a new tab, and that is all.

## Setup

You need Python 3.11 or newer.

Create a virtual environment inside `job_desk/`:

```bash
python -m venv .venv
```

Install the dependencies, including the test tools:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
```

On Windows the interpreter is at `.venv\Scripts\python.exe`. Use that path in
place of `.venv/bin/python` in the commands below.

API keys are not needed yet, because nothing calls a model. When a real client
is added, export `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in your shell. The app
reads them from the environment only. It never writes them to the database or to
logs, and it does not load `.env` files. See `.env.example`.

## Run the dashboard

```bash
.venv/bin/python -m app.main
```

Then open <http://127.0.0.1:8000/entry>.

The server binds to `127.0.0.1` with no override. Requests whose Host header is
not `127.0.0.1` or `localhost` are refused.

### Move jobs along

There is no scheduler yet, so jobs only advance when you run the pipeline. Do
either of these:

- Press **Run pipeline now** on the Working tab.
- Run the orchestrator from the command line:

```bash
.venv/bin/python -m app.orchestrator
```

## Run the tests

```bash
.venv/bin/python -m pytest
```

The tests use temporary data folders. They never touch `data/` or `jobs/` in
this folder, and they make no network calls.

## Where data lives

| Path | What it is |
| --- | --- |
| `data/job_desk.sqlite3` | SQLite database, created on first run |
| `jobs/J-0001/` | One folder per job: `ticket/`, `inputs/`, `model/`, `checks/`, `package/`, `log/` |
| `jobs/events.log` | Append-only JSON lines, one per status change and sign-off action |

Set `JOB_DESK_DATA_DIR` or `JOB_DESK_JOBS_ROOT` to move either location.

## Pipeline

```
draft -> intake -> ticket_ready -> building -> checking -> review -> approved -> delivered -> paid
                                                                  ^
side states: needs_input (operator must act), failed (hard stop)
```

| Stage | Runs at | Moves to | Current behaviour |
| --- | --- | --- | --- |
| `intake` | intake | ticket_ready | Fake responder builds a ticket from the typed measurements. The ticket is validated, and every typed number must match unchanged. Failure goes to needs_input. |
| `build` | ticket_ready | building | Placeholder. Refuses to run while `BUILDER_MODEL` is None, marks the job failed with "builder not configured", and makes no API call. |
| `fusion_bridge` | building | checking | Reads `model/measurements.json`, which you export from Fusion by hand. If it is missing, the job goes to needs_input. |
| `checks` | checking | (stays) | Compares measured values with the bracket fixture and typed values. Writes the `checks` table and `checks/run_NNN.json`. |
| `spec_review` | checking | (stays) | Fake responder writes `checks/spec_review_NNN.md`. |
| `packaging` | checking | review | Copies the model and check outputs into `package/pkg_NNN/` and writes `report.md`. Refuses to run without check results. |

Each stage gets up to two retries after its first attempt. Each retry is counted
on the job. Once the retries are used up, the job goes to needs_input.

Only the orchestrator and sign-off actions can change status. Every change goes
through the single transition table in `app/state.py`. Anything not in that table
raises an error.

## Sign-off actions (job detail page)

- **Approve**: review to approved. Refused unless every check in the latest run
  passed.
- **Send back with notes**: review to ticket_ready. Notes go to
  `ticket/revision_NNN.txt`, and the revision count goes up by one.
- **Mark delivered**, then **Mark paid**.
- **Resume**: needs_input back to the stage that blocked. Use it after you fix
  the cause, for example by saving the Fusion export.
- **Retry**: failed back to ticket_ready.
- **Pause job**: the orchestrator skips the job until you unpause it.

**Pause new jobs** in the header toggles the kill switch. While it is on, the
orchestrator and every model call are refused. This stops in-flight jobs as
well, because their next stage may make a model call.

## Project layout

```
job_desk/
  app/
    main.py          FastAPI routes
    config.py        model config, pricing, cap defaults, paths
    db.py            schema, migrations, query helpers
    state.py         status enum and allowed transitions
    jobs.py          job creation and folder layout
    orchestrator.py  run_once() loop
    signoff.py       human sign-off actions (approve, send back, pause, kill switch)
    api_client.py    single wrapper for all model calls (FakeClient only)
    finance.py       manual entries, Fiverr CSV import, monthly totals
    events.py        append-only JSON-lines event log
    stages/          intake, build, fusion_bridge, checks, spec_review, packaging
    templates/       one template per tab, plus job detail
    static/          CSS only
  tests/
  requirements.txt
  requirements-dev.txt
  .env.example
  TODO.md
```

See [TODO.md](TODO.md) for everything still stubbed and the open questions.
