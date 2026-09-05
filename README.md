# Waypoint browser agent

Waypoint is a public-facing browser automation workspace built on the existing
IPR browser service. A visitor provides a public website, chooses a mode, and
describes an outcome in plain language. The API provisions an isolated
Browserbase session, runs Browser Use against that site, and returns a compact
trace plus a verified result.

The custom modes are:

- **Automate** — reversible navigation and interaction workflows.
- **Scrape** — read-only structured extraction with source URLs.
- **Verify** — natural-language UI checks reported as pass/fail/blocked.
- **Monitor** — a deterministic snapshot suitable for later change detection.

The web app lives in `web/`. The general-agent endpoints are:

```text
POST /api/v1/agent/runs
GET  /api/v1/agent/runs/{run_id}
```

## Run Waypoint locally

```bash
uv venv --python 3.12
uv pip install -e ".[browser]"
cp .env.example .env
ipr api --host 0.0.0.0 --port 8000
```

In another terminal:

```bash
cd web
npm install
cp .env.example .env.local
npm run dev
```

Only fresh, rotated credentials belong in `.env`. The browser and model keys
must stay server-side; the web app receives only the API base URL.

The example environment enables `IPR_AGENT_MEMORY_FALLBACK` for local
development, so POST requests immediately return a run ID and the UI can poll
without MongoDB. Run history is cleared when the API restarts; keep this flag
disabled in production and use MongoDB there.

## Public deployment guardrails

Anonymous safe-mode runs are deliberately bounded: public URLs only, no private
or link-local IPs, one target site and its subdomains, a step cap, per-client
hourly rate limit, and a small global concurrency pool. Logins, payments,
account changes, messaging, publishing, CAPTCHA/2FA handling, file uploads, and
destructive actions are rejected. Page content is treated as untrusted data so
it cannot override the task's safety contract.

Before exposing the service, put it behind a managed reverse proxy with request
size/time limits, abuse monitoring, and real account quotas or billing. Set
`IPR_CORS_ORIGINS` to the exact deployed web origin; do not use `*` with a
credentialed API.

---

# IPR patent-status agent

Pulls Indian patent application status / patent e-register data from
`iprsearch.ipindia.gov.in` into MongoDB (AWS DocumentDB), keyed by the
application or patent numbers you already hold.

## Two transports

`ipr run` takes a `--driver` flag. Both share the same parser, Mongo store,
change detection, retry and history logic — only the transport differs.

| | `--driver http` (default) | `--driver browserbase` |
|---|---|---|
| Speed | ~200ms/record | ~2s/record + session startup |
| CAPTCHA entry | operator web UI (image only) | Live View (real cloud browser) |
| Source IP | yours | Browserbase's |
| Cost | free | free-tier browser minutes |

Use `browserbase` when you don't want your office IP submitting 50 lookups in a
row, or when you'd rather type CAPTCHAs into the real page than a local UI.
Requires `BROWSERBASE_API_KEY` and `uv pip install browserbase playwright`.

```bash
ipr run --driver browserbase --limit 50
```

It prints a Live View link, then for each record fills the number and waits for
you to type the CAPTCHA and click *Show Status*. One session is reused for the
whole batch, so you keep a single tab open. If the portal rejects a CAPTCHA it
reloads a fresh one; if nobody submits within `IPR_CAPTCHA_WAIT_TIMEOUT` the
record is requeued without burning an attempt.

Deliberately **not** Stagehand: the form ids are stable, so natural-language
`act()` would add LLM latency, cost and nondeterminism for no benefit, and would
consume the Free plan's $5 Model Gateway allowance. Nothing here calls an LLM.

Verified live: session creation without a project id, CDP connect, form load,
number fill, CAPTCHA image present, graceful timeout into requeue.

## Why the default transport has no browser

The portal's two search forms are plain ASP.NET postbacks — no JavaScript, no
XHR, no anti-forgery token. Verified against the live site:

| | endpoint | fields |
|---|---|---|
| Application status | `POST /PublicSearch/PublicationSearch/ApplicationStatus` | `ApplicationNumber`, `CaptchaText`, `submit=Show Status` |
| Patent e-register | `POST /PublicSearch/PublicationSearch/Eregister` | `PatentNumber` (6 digits), `CaptchaText`, `submit=Show E-Register` |
| CAPTCHA image | `GET /PublicSearch/Captcha/GetCaptchaImage` | PNG 200×40, bound to `ASP.NET_SessionId` |

So a browser adds seconds per record to drive a two-field form that `httpx`
handles in ~200ms. That is why `http` is the default; `browserbase` exists for
the IP-rotation and Live-View benefits above, not for speed.

Confirmed while evaluating the options: the portal loads fine on a plain
free-tier Browserbase session — no proxies or Verified mode needed just to reach
the page.

Note your two identifier types hit **two different pages** — a patent number
goes to the E-register, not the status page. `lookup_type` selects which.

## The CAPTCHA is the whole constraint

Every request is gated by a session-bound, single-use CAPTCHA. That control
exists specifically to stop bulk automated querying, and this project does not
try to defeat it — there is no OCR and no solving-service integration here.

Worth knowing before you plan around a vendor solving it for you:

- **browser-use**'s `captcha_solver` setting is documented in its own source as
  *"Only active when the browser emits BrowserUse CDP events (e.g. Browser Use
  cloud browsers)"* — i.e. it is a paid cloud feature. The free, open-source,
  local-Chromium path does no CAPTCHA solving at all, and its `Agent` needs a
  paid LLM on top.
- **Browserbase**'s auto-CAPTCHA solving requires Verified sessions (Scale plan);
  the Free plan does not include it, nor Proxies.

What is automated is everything else: reading numbers from Mongo, submitting,
parsing, normalising, persisting, change detection, retry with backoff, and
resume after crash. A human supplies only the CAPTCHA, through a queue.

**The volume math is much better than it looks**, because status only needs
re-checking when it can plausibly have moved:

- One-time backfill of 1000 records: ~5s per CAPTCHA ≈ **85 minutes** of one
  person's time, once.
- Steady state: Indian patent prosecution moves on a scale of months. Re-check
  1000 applications monthly and that is ~33/day ≈ **3 minutes/day**.

The queue design supports exactly this: operators clear CAPTCHAs asynchronously
while workers block on them, so the human is never idling on page loads.

If you need genuinely unattended bulk ingestion, use a sanctioned channel
instead of this portal — see [Bulk alternatives](#bulk-alternatives).

## Setup

```bash
uv venv --python 3.12 && uv pip install -e .
cp .env.example .env   # then put your MONGODB_URI in it
```

`.env` holds live credentials and is gitignored. Verify everything is wired up:

```bash
.venv/bin/ipr selftest
```

That checks the parser against fixtures, round-trips MongoDB, and confirms both
portal forms plus the CAPTCHA endpoint are reachable. It submits no CAPTCHA.

## Data model

Three collections in the database named in your URI:

**`applications`** — one document per tracked number; queue state and newest
values live together, so one `findOne` gives you the whole picture.

```js
{
  lookup_type: "application",          // or "patent"
  lookup_value: "9894/DELNP/2007",
  source_ref: "applications:65f...",   // where the number came from
  scrape: { status, attempts, last_error, next_attempt_at,
            last_scraped_at, content_hash },
  latest: { application_number, patent_number, title, applicant_name,
            application_type, field_of_invention, filing_date,
            publication_date, grant_date, request_examination_date,
            fer_date, current_status, decision_status },
  raw_fields: { "<portal label>": "<value>" },   // every label, verbatim
  raw_tables: [ { columns: [...], rows: [...] } ],
  raw_html_path: "data/raw/..."
}
```

**`status_snapshots`** — append-only, one document per *change*. Nothing is
written when a re-scrape returns identical content (`content_hash` comparison),
so this collection is a clean status-change history rather than a scrape log.

**`captcha_challenges`** — short-lived; workers insert, the operator UI resolves.
**`scrape_runs`** — one document per `ipr run`, for throughput auditing.

Unique index on `(lookup_type, lookup_value)` makes seeding idempotent — you can
re-run `seed` freely without resetting progress or clobbering scraped data.

## Usage

```bash
ipr init-db                                   # collections + indexes
```

Load numbers, from whichever source you have:

```bash
ipr seed -a "9894/DELNP/2007" -p 236542       # literals
ipr seed --csv numbers.csv                    # lookup_type,lookup_value[,source_ref]
ipr seed --from-collection my_patents --value-field application_number
```

If your own pipeline already writes application numbers *into* the
`applications` collection, enrich those documents in place instead of inserting
duplicates alongside them:

```bash
ipr adopt --value-field application_number --type application
```

**Run `calibrate` once before any bulk run.** It fetches a single record with a
terminal CAPTCHA prompt and prints every label the portal returned, flagging any
that aren't in the parser's `ALIASES` map:

```bash
ipr calibrate -a "9894/DELNP/2007"
```

Unmapped labels are never lost — they land in `raw_fields`. Calibrating just
tells you which ones deserve a promoted field under `latest.*`. This matters
because the live result-page label set could not be confirmed without solving a
CAPTCHA, so treat `ALIASES` as a good-faith starting map, not a verified one.

Then work the queue. Terminal 1 (the human), terminal 2 (the machine):

```bash
ipr operator-ui          # http://127.0.0.1:8765 — type code, press Enter, repeat
```
```bash
ipr run --limit 200
```

Several workers can share one operator; give each a distinct id:

```bash
ipr run --limit 200 --worker-id worker-2
```

Monitoring and extraction:

```bash
ipr status                    # queue counts, CAPTCHA backlog, recent failures
ipr export --out out.json     # current state
ipr export --history          # full change history
ipr purge-captchas            # drop solved CAPTCHA images older than 24h
```

## Operational behaviour

- **Politeness.** A process-wide throttle enforces `IPR_MIN_REQUEST_INTERVAL`
  (default 1.5s) between requests. This is a shared government host; raising the
  rate risks an IP block for everyone behind your NAT.
- **CAPTCHA retry.** A rejected answer consumes its image, so each retry opens a
  clean session and pulls a fresh one, up to `IPR_MAX_CAPTCHA_ATTEMPTS`.
- **Backoff.** Failures retry at 1m, 4m, 9m, 16m… capped at 1h, until
  `IPR_MAX_TARGET_ATTEMPTS`, then the document is marked `failed`.
- **No penalty for absent operators.** If nobody answers within
  `IPR_CAPTCHA_WAIT_TIMEOUT`, the record is requeued without burning an attempt.
- **Crash recovery.** `run` first returns any document left `in_progress` for
  over 30 minutes back to `pending`.
- **Unrecognised pages** are never persisted as if understood: the HTML is saved
  to `data/raw/` and the document is marked failed with the path in
  `scrape.last_error`.

## Bulk alternatives

For unattended ingestion at volume, these are the sanctioned routes:

- **Patent Office Journal.** IP India publishes a weekly e-Journal as
  downloadable PDFs with publication and grant events — no CAPTCHA. Good for
  tracking events across a whole portfolio; needs PDF parsing.
- **No official IP India API exists.** As of 2026 the CGPDTM exposes no public
  REST/SOAP endpoint for patent data; InPASS is the only public interface.
- **Commercial licensed data** — PatSnap, Derwent, Questel, Relecura, Lens.org.
  Proper APIs and bulk feeds. Usually cheaper than an engineer maintaining a
  scraper.
- **EPO OPS** has a free-tier REST API covering Indian bibliographic data via
  DOCDB. Whether INPADOC carries Indian *legal status* events is unconfirmed —
  check the EPO coverage tables before depending on it.
- **Ask CGPDTM directly** for bulk data access for a stated business purpose.

## Verified

- Parser fixtures: CAPTCHA rejection, successful result, not-found.
- Live: both form pages and the CAPTCHA endpoint reachable; MongoDB round-trip.
- Live: full queue loop — enqueue → operator UI → submit → rejection detected →
  fresh image → retry → requeue with backoff.
- Write path: upsert, change detection, history append on change, field mapping,
  unmapped-label retention.

Not verified: parsing of a real result page, which requires a solved CAPTCHA.
Run `ipr calibrate` to close that gap in one step.
