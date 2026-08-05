from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

import typer
from bson import json_util
from rich.console import Console
from rich.table import Table

from . import store
from .captcha import QueueSolver, TerminalSolver
from .config import settings
from .parse import ALIASES, Outcome, _norm_label, parse_response
from .pipeline import fetch_one, run_batch
from .portal import LOOKUPS, PortalSession

app = typer.Typer(add_completion=False, help="IP India application-status scraper.")
console = Console()


@app.callback()
def _bootstrap() -> None:
    """Runs before every command: indexes are cheap and idempotent."""
    store.ensure_indexes()


@app.command("init-db")
def init_db() -> None:
    """Create collections and indexes."""
    created = store.ensure_indexes()
    db = store.database()
    console.print(f"[green]ready[/] db=[bold]{db.name}[/]")
    for name in created:
        console.print(f"  index {name}")
    console.print(f"  collections: {sorted(db.list_collection_names())}")


@app.command()
def seed(
    csv_path: Optional[Path] = typer.Option(
        None, "--csv", help="CSV with columns lookup_type,lookup_value[,source_ref]"
    ),
    from_collection: Optional[str] = typer.Option(
        None, "--from-collection", help="Read numbers out of another collection"
    ),
    value_field: str = typer.Option(
        "application_number", "--value-field", help="Field holding the number"
    ),
    type_field: Optional[str] = typer.Option(
        None, "--type-field", help="Field holding 'application'/'patent'"
    ),
    lookup_type: str = typer.Option(
        "application", "--type", help="Used when --type-field is absent"
    ),
    application: list[str] = typer.Option([], "--application", "-a"),
    patent: list[str] = typer.Option([], "--patent", "-p"),
) -> None:
    """Queue numbers to look up.

    Sources are additive: literals, a CSV, and/or another collection.
    Re-seeding is safe — existing documents keep their progress and data.
    """
    rows: list[tuple[str, str, str | None]] = []
    rows += [("application", v, "cli") for v in application]
    rows += [("patent", v, "cli") for v in patent]

    if csv_path:
        with csv_path.open(newline="", encoding="utf-8") as fh:
            for record in csv.DictReader(fh):
                rows.append(
                    (
                        record.get("lookup_type") or lookup_type,
                        record.get("lookup_value", ""),
                        record.get("source_ref"),
                    )
                )

    if from_collection:
        if from_collection == settings.collection_applications:
            raise typer.BadParameter(
                f"--from-collection {from_collection!r} is this tool's own "
                f"collection; use `ipr adopt` to enrich those docs in place"
            )
        coll = store.database()[from_collection]
        projection = {value_field: 1}
        if type_field:
            projection[type_field] = 1
        for doc in coll.find({value_field: {"$nin": [None, ""]}}, projection):
            rows.append(
                (
                    str(doc.get(type_field) or lookup_type) if type_field else lookup_type,
                    str(doc[value_field]),
                    f"{from_collection}:{doc['_id']}",
                )
            )

    if not rows:
        raise typer.BadParameter(
            "nothing to seed; pass --csv, --from-collection, -a or -p"
        )

    added = skipped = invalid = 0
    for lt, value, ref in rows:
        lt, value = lt.strip().lower(), str(value).strip()
        if lt not in LOOKUPS or not value:
            invalid += 1
            continue
        if store.upsert_target(lt, value, ref):
            added += 1
        else:
            skipped += 1
    console.print(
        f"[green]seeded {added}[/] new; {skipped} already queued"
        + (f"; [yellow]{invalid} invalid[/]" if invalid else "")
    )


@app.command()
def adopt(
    value_field: str = typer.Option("application_number", "--value-field"),
    lookup_type: str = typer.Option("application", "--type"),
) -> None:
    """Enrich documents already in the applications collection so this tool can
    work them, instead of inserting duplicates alongside them.

    Use when your own pipeline already writes application numbers into
    `applications` and you want status scraping added to those same documents.
    """
    if lookup_type not in LOOKUPS:
        raise typer.BadParameter(f"--type must be one of {sorted(LOOKUPS)}")
    adopted, conflicts = store.adopt_existing(value_field, lookup_type)
    console.print(f"[green]adopted {adopted}[/] existing document(s)")
    if conflicts:
        console.print(
            f"[yellow]{conflicts} skipped[/] — another doc already tracks that "
            f"number (unique index on lookup_type+lookup_value)"
        )


@app.command()
def calibrate(
    application: Optional[str] = typer.Option(None, "--application", "-a"),
    patent: Optional[str] = typer.Option(None, "--patent", "-p"),
) -> None:
    """Fetch ONE record with a terminal CAPTCHA prompt and report exactly which
    labels the portal returned.

    Run this once before a bulk run: it confirms the parser's ALIASES map covers
    the real field names, and shows any label that would fall through to
    raw_fields instead of a typed field.
    """
    if bool(application) == bool(patent):
        raise typer.BadParameter("pass exactly one of --application / --patent")
    lookup_type = "application" if application else "patent"
    lookup_value = application or patent or ""

    with PortalSession() as portal:
        parsed, html = fetch_one(lookup_type, lookup_value, portal, TerminalSolver())

    out = Path(settings.raw_html_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw_path = out / f"calibrate-{lookup_type}.html"
    raw_path.write_text(html, encoding="utf-8")

    console.print(f"\noutcome: [bold]{parsed.outcome.value}[/]")
    console.print(f"raw html: {raw_path}")

    if parsed.outcome is Outcome.CAPTCHA_REJECTED:
        console.print("[red]CAPTCHA was rejected — rerun and retype carefully.[/]")
        raise typer.Exit(1)

    table = Table("portal label", "value", "-> field", title="fields found")
    unmapped = 0
    for label, value in parsed.fields.items():
        column = ALIASES.get(_norm_label(label))
        if column is None:
            unmapped += 1
        table.add_row(
            label,
            value[:70] + ("…" if len(value) > 70 else ""),
            f"[green]{column}[/]" if column else "[yellow]raw_fields only[/]",
        )
    console.print(table)
    console.print(f"tables captured: {len(parsed.tables)}")
    if unmapped:
        console.print(
            f"[yellow]{unmapped} label(s) not in ALIASES.[/] Still saved under "
            f"raw_fields. Add them to src/ipr_agent/parse.py ALIASES for "
            f"dedicated fields under latest.*"
        )
    if parsed.outcome is Outcome.OK:
        console.print("[green]parser handles this page shape.[/]")


@app.command()
def run(
    limit: int = typer.Option(25, "--limit", "-n", help="Max targets this batch"),
    solver_name: str = typer.Option(
        "queue", "--solver", help="queue (operator UI) or terminal (stdin prompt)"
    ),
    worker_id: str = typer.Option("worker-1", "--worker-id"),
    driver: str = typer.Option(
        "browseruse",
        "--driver",
        help="browseruse (auto CAPTCHA via LLM), http (manual CAPTCHA), or browserbase (cloud + Live View)",
    ),
    model: str = typer.Option(
        "gpt-4o",
        "--model",
        help="LLM model for browser-use CAPTCHA solving (only used with --driver browseruse)",
    ),
    headless: bool = typer.Option(
        True,
        "--headless/--headed",
        help="Run browser headless (browseruse only; use --headed to watch)",
    ),
) -> None:
    """Work the queue: fetch, parse and persist status for pending targets."""

    def report(result) -> None:
        icon = {
            Outcome.OK: "[green]ok[/]",
            Outcome.NOT_FOUND: "[yellow]not found[/]",
        }.get(result.outcome, "[red]fail[/]")
        suffix = (
            " [dim](unchanged)[/]"
            if result.outcome is Outcome.OK and not result.changed
            else ""
        )
        err = f" — {result.error}" if result.error else ""
        console.print(f"  {icon} {result.lookup_value}{suffix}{err}")

    if driver == "browseruse":
        from .pipeline import run_batch_browseruse

        console.print(
            f"[bold]browser-use[/] driver: LLM [cyan]{model}[/] solves CAPTCHAs "
            f"({'headless' if headless else 'headed'} browser)"
        )

        counts = run_batch_browseruse(
            limit,
            on_result=report,
            on_notify=lambda m: console.print(f"  [dim]{m}[/]"),
            headless=headless,
            model=model,
        )
        if counts["attempted"] == 0:
            console.print("[dim]no targets due — queue empty or all backing off[/]")
        console.print("\n" + " ".join(f"{k}={v}" for k, v in counts.items()))
        return

    if driver == "browserbase":
        from .pipeline import run_batch_browserbase

        def on_session(live_view: str, dashboard: str) -> None:
            console.print("\n[bold]Open this to type the CAPTCHAs (Live View):[/]")
            console.print(f"  {live_view}")
            console.print(f"replay + logs afterwards: {dashboard}\n")

        counts = run_batch_browserbase(
            limit,
            on_result=report,
            on_notify=lambda m: console.print(f"  [dim]{m}[/]"),
            on_session=on_session,
        )
        if counts["attempted"] == 0:
            console.print("[dim]no targets due — queue empty or all backing off[/]")
        console.print("\n" + " ".join(f"{k}={v}" for k, v in counts.items()))
        return

    solver = QueueSolver(worker_id) if solver_name == "queue" else TerminalSolver()
    if solver_name == "queue":
        console.print(
            f"using the operator queue — make sure [bold]ipr operator-ui[/] is "
            f"running (http://{settings.operator_host}:{settings.operator_port})"
        )

    counts = run_batch(limit, solver, report)
    if counts["attempted"] == 0:
        console.print("[dim]no targets due — queue empty or all backing off[/]")
    console.print(
        "\n" + " ".join(f"{k}={v}" for k, v in counts.items())
    )


@app.command("operator-ui")
def operator_ui(
    host: Optional[str] = typer.Option(None), port: Optional[int] = typer.Option(None)
) -> None:
    """Serve the page where a human clears queued CAPTCHAs."""
    from .operator_ui import serve

    serve(host, port)


@app.command()
def status() -> None:
    """Queue counts, CAPTCHA backlog, and recent failures."""
    counts = store.status_counts()
    table = Table("scrape status", "count")
    for state, n in counts.items():
        table.add_row(state, str(n))
    console.print(table)

    snaps = store.snapshots().count_documents({})
    pending_cap, solved_cap = store.captcha_counts()
    console.print(f"snapshots: [bold]{snaps}[/]")
    console.print(f"captchas: {pending_cap} pending, {solved_cap} solved")

    failures = store.failed_samples()
    if failures:
        fail_table = Table("failed", "attempts", "last error")
        for doc in failures:
            scrape = doc.get("scrape", {})
            fail_table.add_row(
                doc.get("lookup_value", ""),
                str(scrape.get("attempts", 0)),
                (scrape.get("last_error") or "")[:90],
            )
        console.print(fail_table)


@app.command()
def export(
    out: Path = typer.Option(Path("data/export.json"), "--out"),
    history: bool = typer.Option(
        False, "--history", help="Export the snapshot history instead of current state"
    ),
) -> None:
    """Dump current state (or full history) to JSON."""
    coll = store.snapshots() if history else store.applications()
    docs = list(coll.find({}))
    out.parent.mkdir(parents=True, exist_ok=True)
    # json_util handles ObjectId/datetime that plain json cannot.
    out.write_text(
        json.dumps(json.loads(json_util.dumps(docs)), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    console.print(f"[green]wrote {len(docs)} doc(s)[/] to {out}")


@app.command()
def purge_captchas(hours: int = typer.Option(24, "--older-than-hours")) -> None:
    """Delete solved CAPTCHA images older than N hours (they are just clutter)."""
    console.print(f"deleted {store.purge_solved_captchas(hours)} solved challenge(s)")


@app.command("selftest")
def selftest() -> None:
    """Offline parser checks, live MongoDB round-trip, and live reachability of
    the portal's form + CAPTCHA endpoints. Submits no CAPTCHA."""
    from .parse import Outcome as O

    bad = "<html><body><span class='field-validation-error'>Invalid captcha</span></body></html>"
    assert parse_response(bad).outcome is O.CAPTCHA_REJECTED, "captcha marker"

    good = """<html><body><table>
      <tr><td>Application Number</td><td>9894/DELNP/2007</td></tr>
      <tr><td>Application Status</td><td>Application Refused</td></tr>
      <tr><td>Title of Invention</td><td>A WIDGET</td></tr>
      <tr><td>Date Of Filing</td><td>17/12/2007</td></tr>
    </table></body></html>"""
    result = parse_response(good)
    assert result.outcome is O.OK, result.outcome
    assert result.columns["application_number"] == "9894/DELNP/2007"
    assert result.columns["current_status"] == "Application Refused"
    assert result.columns["title"] == "A WIDGET"
    assert result.columns["filing_date"] == "17/12/2007"

    missing = "<html><body><p>No Record Found</p></body></html>"
    assert parse_response(missing).outcome is O.NOT_FOUND, "not-found marker"
    console.print("[green]parser OK[/] (3 fixtures)")

    info = store.client().server_info()
    console.print(
        f"[green]mongo OK[/] {store.database().name} "
        f"(server {info.get('version')})"
    )

    with PortalSession() as portal:
        for lookup_type in ("application", "patent"):
            portal.reset()
            portal.open_form(lookup_type)
            image = portal.fetch_captcha(lookup_type)
            assert image[:8] == b"\x89PNG\r\n\x1a\n", "captcha not a PNG"
            console.print(
                f"[green]{lookup_type} form + captcha reachable[/] "
                f"({len(image)} byte PNG)"
            )


@app.command("api")
def api_server(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
    workers: int = typer.Option(1, "--workers", help="Uvicorn workers (keep at 1 — worker thread is in-process)"),
) -> None:
    """Start the REST API with a built-in background scraping worker.

    The API serves endpoints for submitting batches and polling results.
    A background thread continuously processes the scrape queue using
    Playwright + GPT-4o vision. Everything runs in one process.

    Docs: http://<host>:<port>/docs
    """
    import uvicorn

    console.print(f"[bold green]starting API[/] on http://{host}:{port}")
    console.print(f"  docs: http://{host}:{port}/docs")
    console.print(f"  worker thread starts automatically\n")
    uvicorn.run(
        "ipr_agent.api:app",
        host=host,
        port=port,
        workers=workers,
        log_level="info",
    )


if __name__ == "__main__":
    app()
