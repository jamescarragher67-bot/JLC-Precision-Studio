# TODO

Everything still stubbed, plus the decisions and open questions from the first build.

## Stubbed

- [ ] **Builder (GPT-6 Astra).** `config.BUILDER_MODEL` is `None`, so the build
  stage refuses to run. Still needed: the model id, a verified price entry in
  `config.PRICING` (the entry is empty now), the prompt and output format, and the
  build step itself. `app/stages/build.py` has the TODO.
- [ ] **Real model client.** `intake` and `spec_review` use fake responders. Only
  `FakeClient` exists in `app/api_client.py`. Add a real client, and make it the
  only place that makes network calls. Haiku pricing in `config.PRICING` is marked
  "verify before relying on this" and has not been checked.
- [ ] **Fusion.** The bridge reads `model/measurements.json`, which is exported
  by hand. The export format is a JSON object mapping a measurement name to a
  number. The names are `length`, `width`, `height`, `hole_count`, `hole_diameter`,
  `volume` (mm^3), and `mass` (g). Still needed: how Fusion's API is invoked. The
  spec says to ask before deciding this.
- [ ] **Fiverr CSV format.** `config.FIVERR_CSV_MAPPING` is a guess. Confirm the
  headers, the date format, the sign convention (positive means money in, and
  negative means money out), and the currency with a real export.
- [ ] **Scheduler.** Not built, on purpose. Jobs advance only through the
  **Run pipeline now** button or `python -m app.orchestrator`. Add an OS scheduler
  only after the user approves one.
- [ ] **API spend in finance.** `api_log` entries are not posted to finance yet.
  The USD-to-AUD rate needs a decision first.
- [ ] **Per-part checks.** The check expectations are the bracket fixture only.
  Volume and mass do not apply to other parts.
- [ ] **Revision notes reach the builder.** Send-back notes are saved to
  `ticket/revision_NNN.txt`, but nothing reads them yet.
- [ ] **Ticket-only measurements.** Intake stores measurements that only the
  ticket provides (source `ticket`). The checks do not use them yet.
- [ ] **Monthly cap in the UI.** The cap is the `monthly_api_cap_usd` setting,
  default 100. There is no screen to change it yet.
- [ ] **Authentication.** None. Protection is the loopback bind plus the Host
  check. Add CSRF protection before the dashboard is reachable from anywhere else.
- [ ] **Test-client warning.** Starlette warns that `httpx` should be replaced
  with `httpx2` for `TestClient`. Decide whether to switch.

## Open questions

- Bracket axes: is `length` the X axis (120 mm), `width` Y (40 mm), and `height` Z (45 mm)?
- Should the kill switch also stop in-flight jobs? It does today, and the label
  still reads "Pause new jobs" as the spec asked.
- Are delivered and paid sign-off actions acceptable? The spec lists approve,
  send back, and pause. I added delivered and paid as manual sign-offs.
- Should approval stay blocked whenever any check fails? It does today.
- Is the approximate token count (about 4 characters per token) acceptable for the
  pre-call cap check? Real usage is recorded from the client's reported tokens.

## Decisions and assumptions

- Repository root is `D:\JLC PRECISION STUDIO`, with this project in `job_desk/`.
  The GitHub repo is `JLC-Precision-Studio`.
- Job creation sets status to intake directly. It is not a transition, so the
  table does not include "create".
- The title is the first line of the order text, cut to 80 characters. The form
  has no title field.
- The transition table includes two sign-off transitions that were not in the spec:
  `review -> ticket_ready` (send back) and `needs_input -> <stage>` (resume).
  Failed jobs can be retried with `failed -> ticket_ready`.
- Schema migration 2 adds three columns to `jobs`: `revision_count`, `paused`,
  and `stage_cursor`. The spec did not list them, but they are needed.
- A failed check does not stop packaging. The report shows FAIL, and approval is
  blocked until the job is sent back or the checks pass.
- Each packaging run writes a new `package/pkg_NNN/` folder, so earlier packages
  are never overwritten.
- Retry counting: the first attempt plus two retries. The third failure goes to
  needs_input.
- Pricing: a model with no price in `config.PRICING` is refused, so the cap cannot
  be bypassed by an unpriced call.
- `events.log` is written after the database commit. If the process crashes
  between the two, the log can miss a line. The database is the source of truth.
