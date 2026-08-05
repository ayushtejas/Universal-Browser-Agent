# Handoff brief — ipr-agent

## Goal

Read Indian patent application / patent numbers from MongoDB, look each one up on
the IP India portal, scrape the status page, write results back to MongoDB.
Target: unattended batches of ~50.

## What's already built and working

Python 3.12 project, `uv`, entry point `ipr` (Typer CLI). Persistence is
MongoDB (AWS DocumentDB 5.0).

**Portal facts (verified against the live site):**

| | endpoint | fields |
|---|---|---|
| Application status | `POST /PublicSearch/PublicationSearch/ApplicationStatus` | `ApplicationNumber`, `CaptchaText`, `submit=Show Status` |
| Patent e-register | `POST /PublicSearch/PublicationSearch/Eregister` | `PatentNumber` (6 digits), `CaptchaText`, `submit=Show E-Register` |
| CAPTCHA image | `GET /PublicSearch/Captcha/GetCaptchaImage` | PNG 200×40, bound to `ASP.NET_SessionId`, single-use |
| Wrong CAPTCHA | HTTP 200, page re-renders containing text `Invalid captcha` |

Plain ASP.NET form posts — no JS, no anti-forgery token. Note the two identifier
types hit **two different pages**.

**Files:**

| file | role |
|---|---|
| `portal.py` | HTTP transport (httpx), throttle, session/CAPTCHA handling |
| `driver_browserbase.py` | Browserbase transport — cloud Chrome via Playwright/CDP |
| `parse.py` | HTML → structured fields. Generic label/value + table harvest, `ALIASES` maps labels to typed fields |
| `store.py` | All MongoDB access, atomic claim via `findOneAndUpdate` |
| `pipeline.py` | Orchestration, retry/backoff, change detection |
| `captcha.py` | `CaptchaSolver` protocol + terminal/queue implementations |
| `operator_ui.py` | stdlib web UI for manual CAPTCHA entry |
| `cli.py` | commands |

**Collections:** `applications` (one doc per number: `scrape.*` queue state +
`latest.*` newest values + `raw_fields`/`raw_tables`), `status_snapshots`
(append-only, one doc per *change*), `captcha_challenges`, `scrape_runs`.
Unique index on `(lookup_type, lookup_value)` → seeding is idempotent.

**Commands:**

```bash
ipr init-db
ipr seed -a "9894/DELNP/2007" -p 236542     # or --csv / --from-collection
ipr adopt --value-field application_number  # enrich existing docs in place
ipr calibrate -a "<number>"                 # confirm parser field map
ipr run --limit 50 [--driver http|browserbase]
ipr status / export / selftest
```

**Verified working:** parser fixtures; live Mongo round-trip; both portal forms +
CAPTCHA endpoint reachable; full queue loop (enqueue → submit → `Invalid captcha`
detected → fresh image → retry → requeue with backoff); write path (upsert,
content-hash change detection, history append, field mapping, unmapped-label
retention); Browserbase session creation without a project id, CDP connect, form
load, number fill, graceful timeout.

**Not verified:** parsing of a real result page — needs one solved CAPTCHA.
Run `ipr calibrate` once and extend `ALIASES` in `parse.py` from its output.

## What I want done

**Automate the CAPTCHA step so batches run unattended.** Currently a human types
it (local operator UI, or Browserbase Live View). I want that removed.

Where it plugs in — the transport is already abstracted, so this is a contained
change:

1. `driver_browserbase.py` → `start()`: pass `browser_settings` to
   `bb.sessions.create()` enabling Browserbase's CAPTCHA solving. Requires a
   plan tier that includes Verified sessions — confirm current tier
   requirements at https://docs.browserbase.com/platform/identity/proxies
2. `driver_browserbase.py` → `fetch()`: after filling the number, click
   `input[name='submit']` directly instead of waiting for a human. Drop the
   `notify` callback.
3. `_wait_for_result()` already polls correctly — keep it, just shorten
   `wait_timeout`. It returns on `OK` / `NOT_FOUND` / `CAPTCHA_REJECTED`, so the
   existing retry loop in `fetch()` still works.
4. Nothing in `parse.py`, `store.py` or `pipeline.py` needs to change —
   `process_target(doc, None, None, fetcher=...)` is the seam.

Then `ipr run --driver browserbase --limit 50` should complete with no human.

Also outstanding, independent of the above:
- Run `ipr calibrate` once and extend `parse.py` `ALIASES` from the real labels.
- Consider raising concurrency (Browserbase project allows 3).

## Notes

- `.env` holds `MONGODB_URI` and `BROWSERBASE_API_KEY`; gitignored, chmod 600.
  **Both credentials were pasted in a chat transcript — rotate them.**
- Set `BROWSE_LOAD_DOTENV=0` or the `browse` CLI auto-loads `MONGODB_URI`.
- Keep `IPR_MIN_REQUEST_INTERVAL` ≥ 1.5s. Shared government host; hammering it
  gets the IP blocked.
- No official IP India API exists (as of 2026). Free unattended alternative if
  the above stalls: the weekly Patent Office e-Journal PDFs carry publication /
  examination / grant events with no CAPTCHA — different data shape (events
  portfolio-wide, not on-demand per number), but genuinely cron-able.
