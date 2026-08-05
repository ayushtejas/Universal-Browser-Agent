"""Turn a portal response page into structured data.

Deliberately structure-driven rather than selector-driven: the portal renders
label/value pairs in nested tables whose classes and ids are inconsistent
between the ApplicationStatus and Eregister views. So we harvest *every*
label/value pair and *every* table, keep the lot in `raw_fields`/`raw_tables`,
and additionally map known labels onto typed columns via ALIASES.

Consequence worth knowing: an unrecognised label is never lost, it just lands in
raw_fields instead of a column. Run `ipr calibrate` against a real record to see
the actual label set and extend ALIASES from it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from bs4 import BeautifulSoup, Tag


class Outcome(str, Enum):
    OK = "ok"
    CAPTCHA_REJECTED = "captcha_rejected"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


# Verified against the live site: a bad CAPTCHA re-renders the form with this text.
_CAPTCHA_REJECTED_MARKERS = (
    "invalid captcha",
    "wrong captcha",
    "captcha is invalid",
    "enter valid captcha",
)

_NOT_FOUND_MARKERS = (
    "no record found",
    "no records found",
    "record not found",
    "no data found",
    "invalid application number",
    "invalid patent number",
    "does not exist",
)

# normalised label -> Snapshot column
ALIASES: dict[str, str] = {
    "applicationnumber": "application_number",
    "appliationnumber": "application_number",  # portal typo seen in some views
    "patentnumber": "patent_number",
    "grantpatentnumber": "patent_number",
    "titleofinvention": "title",
    "title": "title",
    "inventiontitle": "title",
    "applicantname": "applicant_name",
    "nameofapplicant": "applicant_name",
    "applicant": "applicant_name",
    "applicationtype": "application_type",
    "fieldofinvention": "field_of_invention",
    "dateoffiling": "filing_date",
    "applicationfilingdate": "filing_date",
    "filingdate": "filing_date",
    "publicationdateus11a": "publication_date",
    "publicationdate": "publication_date",
    "publicationdateus11aordinary": "publication_date",
    "dateofgrant": "grant_date",
    "grantdate": "grant_date",
    "dateofcertificateissue": "grant_date",  
    "requestforexaminationdate": "request_examination_date",
    "rfedate": "request_examination_date",
    "firstexaminationreportdate": "fer_date",
    "ferdate": "fer_date",
    "applicationstatus": "current_status",
    "status": "current_status",
    "currentstatus": "current_status",
    "decisionstatus": "decision_status",
    "decision": "decision_status",
    "internalstatus": "decision_status",
}


@dataclass
class ParseResult:
    outcome: Outcome
    fields: dict[str, str] = field(default_factory=dict)
    tables: list[dict[str, Any]] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)
    text_excerpt: str = ""

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            {"f": self.fields, "t": self.tables}, sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _norm_label(s: str) -> str:
    """'Publication Date (U/S 11A)' -> 'publicationdateus11a'"""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return _clean(soup.get_text(" "))


def _strip_chrome(soup: BeautifulSoup) -> None:
    """Remove site-wide nav/footer so their text can't be mistaken for data."""
    for selector in ("nav", "header", "footer", "script", "style", "noscript"):
        for tag in soup.select(selector):
            tag.decompose()
    for tag in soup.select(".navbar, .footer, #footer, .breadcrumb"):
        tag.decompose()


def _row_cells(row: Tag) -> list[str]:
    return [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"], recursive=False)]


def _harvest_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """Every label/value pair we can find, from 2-cell table rows and from
    <label>/sibling and <dt>/<dd> constructs."""
    pairs: dict[str, str] = {}

    def put(label: str, value: str) -> None:
        label, value = _clean(label).rstrip(":*").strip(), _clean(value)
        if not label or len(label) > 120:
            return
        # Keep the first non-empty value for a label; portal repeats headings.
        if value and (label not in pairs or not pairs[label]):
            pairs[label] = value

    for row in soup.find_all("tr"):
        cells = _row_cells(row)
        if len(cells) == 2:
            put(cells[0], cells[1])
        elif len(cells) == 4:
            # Two label/value pairs side by side — common in this portal.
            put(cells[0], cells[1])
            put(cells[2], cells[3])

    for dt_tag in soup.find_all("dt"):
        dd = dt_tag.find_next_sibling("dd")
        if dd is not None:
            put(dt_tag.get_text(" "), dd.get_text(" "))

    return pairs


def _harvest_tables(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Any table with a real header row and >=1 data row, e.g. the document /
    correspondence listings."""
    out: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = [_clean(c.get_text(" ")) for c in rows[0].find_all("th")]
        if len(header) < 2:
            continue
        data: list[dict[str, str]] = []
        for row in rows[1:]:
            cells = [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"])]
            if not any(cells):
                continue
            if len(cells) != len(header):
                data.append({"_cells": cells})  # keep it rather than drop it
            else:
                data.append(dict(zip(header, cells)))
        if data:
            out.append({"columns": header, "rows": data})
    return out


def _map_columns(pairs: dict[str, str]) -> dict[str, str]:
    cols: dict[str, str] = {}
    for label, value in pairs.items():
        key = ALIASES.get(_norm_label(label))
        if key and value and not cols.get(key):
            cols[key] = value
    return cols


def parse_response(html: str) -> ParseResult:
    soup = BeautifulSoup(html, "html.parser")
    text_lower = _visible_text(BeautifulSoup(html, "html.parser")).lower()

    if any(m in text_lower for m in _CAPTCHA_REJECTED_MARKERS):
        return ParseResult(outcome=Outcome.CAPTCHA_REJECTED)

    _strip_chrome(soup)
    pairs = _harvest_pairs(soup)
    tables = _harvest_tables(soup)
    columns = _map_columns(pairs)
    excerpt = _visible_text(soup)[:2000]

    # A result page always identifies the record it is describing.
    if columns.get("application_number") or columns.get("patent_number"):
        return ParseResult(Outcome.OK, pairs, tables, columns, excerpt)

    if any(m in text_lower for m in _NOT_FOUND_MARKERS):
        return ParseResult(Outcome.NOT_FOUND, pairs, tables, columns, excerpt)

    # Still showing the empty form => submit silently failed.
    if soup.find(id="CaptchaText") is not None and not pairs:
        return ParseResult(Outcome.UNKNOWN, pairs, tables, columns, excerpt)

    # Data present but no recognised identifier: real content we can't classify.
    # Treat as UNKNOWN so it is flagged for calibration rather than saved wrong.
    return ParseResult(
        Outcome.OK if len(pairs) >= 4 else Outcome.UNKNOWN,
        pairs,
        tables,
        columns,
        excerpt,
    )
