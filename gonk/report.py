"""Render a run result as an email-ready HTML report, a plain-text summary, and CSVs.

Email clients strip scripts and most CSS, so charts are drawn with inline-styled
tables and divs. Everything uses inline styles, which Gmail, Outlook and Apple Mail
all render.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape

from .catalog import RECORD_FIELDS
from . import confidence as conf
from .pipeline import RunResult

# Palette (validated reference palette: categorical slots 1-2 for new/removed).
SURFACE = "#fcfcfb"
PAGE = "#f1f0ec"
TEXT = "#0b0b0b"
TEXT2 = "#52514e"
MUTED = "#7a7974"
RULE = "#e3e1db"
TRACK = "#ecebe6"
NEW = "#2a78d6"
GONE = "#eb6834"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

ROWS_PER_LAUNCH = 12
MAX_LAUNCHES = 25
MAX_REMOVED_ROWS = 60
MAX_HTML_BYTES = 95_000
# (rows per launch, launches, removed rows), tried in order until the email fits.
SIZE_STEPS = [(12, 25, 60), (8, 20, 40), (5, 15, 30), (3, 12, 20), (2, 10, 15), (0, 8, 10)]
NEW_SLOT, GONE_SLOT = "\x00NEW_TABLES\x00", "\x00GONE_TABLES\x00"


@dataclass
class Report:
    subject: str
    html: str
    text: str
    attachments: dict[str, str | bytes] = field(default_factory=dict)  # filename -> CSV text or zip bytes


# -- small helpers ---------------------------------------------------------
def e(v) -> str:
    return escape("" if v is None else str(v))


def fnum(v, digits=0) -> str:
    if v is None:
        return "–"
    return f"{v:,.{digits}f}"


def orbit_text(r: dict) -> str:
    regime = r.get("regime") or "Unknown"
    if r.get("sso"):
        regime += " · SSO"
    return regime


def alt_text(r: dict) -> str:
    if r.get("perigee_km") is None or r.get("apogee_km") is None:
        return "pending"
    return f"{fnum(r['perigee_km'])} × {fnum(r['apogee_km'])} km"


def section(title: str, body: str, subtitle: str = "") -> str:
    sub = f'<div style="color:{TEXT2};font-size:13px;margin:2px 0 12px">{subtitle}</div>' if subtitle else '<div style="height:10px"></div>'
    return (f'<tr><td style="padding:24px 28px 4px">'
            f'<div style="font-size:17px;font-weight:600;color:{TEXT}">{e(title)}</div>{sub}{body}</td></tr>')


def kpi_row(tiles: list[tuple[str, str, str]]) -> str:
    """tiles: (label, value, sub-note)."""
    width = int(100 / len(tiles))
    cells = "".join(
        f'<td width="{width}%" valign="top" style="padding:14px 12px;border:1px solid {RULE};border-radius:8px;background:#fff">'
        f'<div style="font-size:12px;color:{TEXT2}">{e(label)}</div>'
        f'<div style="font-size:24px;font-weight:600;color:{TEXT};margin-top:4px">{e(value)}</div>'
        f'<div style="font-size:12px;color:{MUTED};margin-top:2px">{e(note)}</div></td>'
        for label, value, note in tiles)
    return f'<table role="presentation" width="100%" cellspacing="8" cellpadding="0" style="margin:0 -8px"><tr>{cells}</tr></table>'


def hbar(title: str, items: list[tuple[str, int]], color: str, top: int = 8, by_value: bool = True) -> str:
    """Single-series horizontal bar chart. Extra categories fold into 'Other'."""
    items = [i for i in items if i[1] > 0]
    if not items:
        return ""
    if by_value:
        items.sort(key=lambda kv: -kv[1])
    if len(items) > top:
        keep = [kv for kv in items if kv[0] != "Other"][:top - 1]
        rest = sum(v for k, v in items if (k, v) not in keep)
        items = keep + [("Other", rest)]
    vmax = max(v for _, v in items)
    rows = []
    for label, v in items:
        pct = max(1, round(100 * v / vmax))
        rows.append(
            f'<tr><td style="font-size:12px;color:{TEXT2};padding:3px 8px 3px 0;white-space:nowrap;width:38%">{e(label)}</td>'
            f'<td style="padding:3px 0"><table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td width="{pct}%" style="background:{color};height:12px;border-radius:0 4px 4px 0;font-size:0;line-height:0">&nbsp;</td>'
            f'<td style="font-size:12px;color:{TEXT};padding-left:6px;white-space:nowrap">{v:,}</td></tr></table></td></tr>')
    return (f'<div style="font-size:13px;font-weight:600;color:{TEXT};margin:4px 0 6px">{e(title)}</div>'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(rows)}</table>')


def two_up(left: str, right: str) -> str:
    if not right:
        return left
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td width="50%" valign="top" style="padding-right:14px">{left}</td>'
            f'<td width="50%" valign="top" style="padding-left:14px">{right}</td></tr></table>')


def trend_chart(history: list[dict], days: int = 30) -> str:
    """Grouped vertical bars: new (blue) vs removed (orange) per daily run."""
    runs = [h for h in history if h.get("kind") == "daily"][-days:]
    if len(runs) < 2:
        return ""
    vmax = max(max(h["new"], h["gone"]) for h in runs) or 1
    height = 90

    def bar(v, color):
        px = 0 if v == 0 else max(2, round(height * v / vmax))
        inner = (f'<div style="height:{px}px;background:{color};border-radius:3px 3px 0 0;font-size:0;line-height:0">&nbsp;</div>'
                 if px else "")
        return (f'<td valign="bottom" style="height:{height}px;width:6px;padding:0 1px" title="{v}">{inner}</td>')

    cols = "".join(
        f'<td valign="bottom" style="padding:0 2px"><table role="presentation" cellspacing="0" cellpadding="0"><tr>'
        f'{bar(h["new"], NEW)}{bar(h["gone"], GONE)}</tr></table></td>' for h in runs)
    first, last = runs[0]["date"], runs[-1]["date"]
    tot_new, tot_gone = sum(h["new"] for h in runs), sum(h["gone"] for h in runs)
    legend = (f'<span style="display:inline-block;width:10px;height:10px;background:{NEW};border-radius:2px"></span> '
              f'New ({tot_new:,}) &nbsp;&nbsp;'
              f'<span style="display:inline-block;width:10px;height:10px;background:{GONE};border-radius:2px"></span> '
              f'Removed ({tot_gone:,})')
    return (f'<div style="font-size:12px;color:{TEXT2};margin-bottom:8px">{legend} · peak {vmax:,}/day</div>'
            f'<table role="presentation" cellspacing="0" cellpadding="0" style="border-bottom:1px solid {RULE}"><tr>{cols}</tr></table>'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>'
            f'<td style="font-size:11px;color:{MUTED};padding-top:4px">{e(first)}</td>'
            f'<td align="right" style="font-size:11px;color:{MUTED};padding-top:4px">{e(last)}</td></tr></table>')


def table(headers: list[str], rows: list[list[str]], note: str = "") -> str:
    th = "".join(f'<th align="left" style="font-size:11px;font-weight:600;color:{TEXT2};padding:6px 8px;'
                 f'border-bottom:1px solid {RULE};white-space:nowrap">{e(h)}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td valign="top" style="font-size:12px;color:{TEXT};padding:5px 8px;'
                         f'border-bottom:1px solid {RULE}">{c}</td>' for c in row) + "</tr>"
        for row in rows)
    extra = f'<div style="font-size:12px;color:{MUTED};padding:6px 8px">{note}</div>' if note else ""
    return (f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            f'style="border-collapse:collapse;background:#fff;border:1px solid {RULE};border-radius:6px">'
            f'<tr>{th}</tr>{body}</table>{extra}')


def plural(word: str, n: int) -> str:
    if n == 1:
        return word
    if word.endswith("y"):
        return word[:-1] + "ies"
    return word if word.endswith("s") else word + "s"


BARS = {conf.HIGH: 3, conf.MEDIUM: 2, conf.LOW: 1}


def meter(level: str) -> str:
    """Three-bar signal meter: the number of filled bars encodes the level (not color alone)."""
    filled = BARS[level]
    bars = "".join(
        f'<span style="display:inline-block;width:4px;height:{6 + 2 * i}px;margin-right:1px;'
        f'vertical-align:bottom;border:1px solid {TEXT2};background:{TEXT2 if i < filled else "#fff"}"></span>'
        for i in range(3))
    return f'<span style="display:inline-block;vertical-align:baseline">{bars}</span>'


def conf_label(level: str) -> str:
    weight = "600" if level == conf.LOW else "400"
    return f'<span style="white-space:nowrap;font-weight:{weight}">{meter(level)} {level.title()}</span>'


def conf_cell(level: str | None, basis: str = "") -> str:
    if not level:
        return f'<span style="color:{MUTED}">–</span>'
    out = conf_label(level)
    if basis:
        out += f'<div style="font-size:11px;color:{TEXT2};margin-top:2px">{e(basis)}</div>'
    return out


def conf_counts(records: list[dict]) -> str:
    c = Counter(r.get("confidence") for r in records if r.get("confidence"))
    parts = [f"{conf_label(k)} {c[k]:,}" for k in (conf.HIGH, conf.MEDIUM, conf.LOW) if c.get(k)]
    return " &nbsp;·&nbsp; ".join(parts)


def method_html() -> str:
    rows = [[f"<b>{e(src)}</b>", e(what), e(rule)] for src, what, rule in conf.METHOD]
    return table(["Source", "Provides", "How it is rated"], rows)


def badge(text: str, color: str = TEXT2) -> str:
    return (f'<span style="display:inline-block;font-size:11px;color:{color};border:1px solid {RULE};'
            f'border-radius:10px;padding:0 6px;margin-left:4px;white-space:nowrap">{e(text)}</span>')


def wrap(title: str, subtitle: str, sections: list[str], footer: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};font-family:{FONT};color:{TEXT}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{PAGE}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:760px;background:{SURFACE};border:1px solid {RULE};border-radius:10px">
<tr><td style="padding:26px 28px 6px">
<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{TEXT2}">Gonk orbital report</div>
<div style="font-size:24px;font-weight:650;color:{TEXT};margin-top:4px">{e(title)}</div>
<div style="font-size:13px;color:{TEXT2};margin-top:4px">{subtitle}</div></td></tr>
{''.join(s for s in sections if s)}
<tr><td style="padding:22px 28px 26px;font-size:11px;color:{MUTED};line-height:1.5;border-top:1px solid {RULE}">{footer}</td></tr>
</table></td></tr></table></body></html>"""


FOOTER = ("Data: Space-Track.org (U.S. Space Force, 18th/19th Space Defense Squadrons); radio frequencies, "
          "where shown, from the SatNOGS DB community database, which is not an official source. Every fact "
          "carries a source and a High/Medium/Low confidence rating; the attached CSVs list both, with the "
          "reason, for each field. “New” means newly added to the public "
          "catalog — new launches typically take days to weeks to be cataloged, and occasionally "
          "long-tracked older objects (such as debris) are cataloged for the first time. Orbits for new "
          "objects come from their first published element sets and will refine over time. "
          "SSO = sun-synchronous. Full details for every object are in the attached CSV files.")


# -- CSV -------------------------------------------------------------------
CSV_EXTRA = ["decay_epoch", "decay_source", "days_in_orbit", "removal_reason",
             "predicted_epoch", "prediction_source"]


# Fixed column order for every CSV Gonk writes, so daily files line up with each other
# and with the baseline. Columns that don't apply to a record are left empty.
CSV_COLUMNS = (RECORD_FIELDS + CSV_EXTRA + ["confidence", "confidence_basis"]
               + [f"{g}_{k}" for g in conf.RATED_GROUPS for k in ("confidence", "source", "basis")]
               # Columns added later go at the end, so older daily files still line up.
               + ["added"])


def to_csv(records: list[dict], leading: dict[str, str] | None = None) -> str:
    """CSV with CSV_COLUMNS; `leading` adds constant columns first (e.g. run_date)."""
    buf = io.StringIO()
    lead = leading or {}
    w = csv.DictWriter(buf, fieldnames=list(lead) + CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in records:
        row = {**lead, **r, **conf.flatten(r)}
        if isinstance(row.get("rf"), list):
            row["rf"] = "; ".join(f'{x["text"]} [{x["confidence"]}: {x["basis"]}]' for x in row["rf"])
        w.writerow(row)
    return buf.getvalue()


def zipped(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return buf.getvalue()


# -- composition charts ------------------------------------------------------
def composition(objects: dict[int, dict]) -> str:
    recs = list(objects.values())
    by_country = Counter(r.get("country") or r.get("country_code") or "Unknown" for r in recs)
    payloads = [r for r in recs if r.get("type") == "PAYLOAD"]
    by_country_pay = Counter(r.get("country") or r.get("country_code") or "Unknown" for r in payloads)
    by_regime = Counter(r.get("regime") or "Unknown" for r in payloads)
    by_const = Counter(r["constellation"] for r in payloads if r.get("constellation"))
    return (two_up(hbar("Payloads by country", list(by_country_pay.items()), NEW),
                   hbar("All objects by country", list(by_country.items()), NEW))
            + '<div style="height:16px"></div>'
            + two_up(hbar("Payloads by orbit", list(by_regime.items()), NEW),
                     hbar("Largest constellations (payloads)", list(by_const.items()), NEW)))


def type_counts(objects: dict[int, dict]) -> Counter:
    return Counter(r.get("type") or "UNKNOWN" for r in objects.values())


# -- new-object tables --------------------------------------------------------
def launch_groups(new: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for r in new:
        groups.setdefault(r.get("launch_id") or f"unknown-{r['norad']}", []).append(r)
    return sorted(groups.items(), key=lambda kv: (kv[1][0].get("launch_date") or "", kv[0]), reverse=True)


def new_objects_html(new: list[dict], run_at: datetime, rows_per_launch: int = ROWS_PER_LAUNCH,
                     max_launches: int = MAX_LAUNCHES) -> str:
    out = []
    groups = launch_groups(new)
    for lid, recs in groups[:max_launches]:
        first = recs[0]
        types = Counter(r.get("type", "UNKNOWN").lower() for r in recs)
        type_txt = ", ".join(f"{n} {plural(t, n)}" for t, n in types.most_common())
        consts = {r.get("constellation") for r in recs if r.get("constellation")}
        head = (f'<b>{e(lid)}</b> · launched {e(first.get("launch_date", "?"))}'
                f' from {e(first.get("site") or first.get("site_code") or "?")}'
                f' · {e(first.get("country") or first.get("country_code") or "?")} · {e(type_txt)}')
        if consts:
            head += " · " + e(", ".join(sorted(consts)))
        ld = first.get("launch_date")
        if ld and ld < (run_at - timedelta(days=60)).date().isoformat():
            head += badge("late catalog entry", TEXT2)
        rows = []
        for r in sorted(recs, key=lambda r: r["norad"])[:rows_per_launch]:
            name = e(r.get("name"))
            if r.get("decay_date"):
                name += badge("already re-entered", GONE)
            rf = "<br>".join(f'{e(x["text"])} {conf_label(x["confidence"])}'
                             for x in (r.get("rf") or [])[:2]) or f'<span style="color:{MUTED}">none listed</span>'
            rows.append([
                e(r["norad"]), name, e((r.get("type") or "").title()), e(orbit_text(r)), e(alt_text(r)),
                fnum(r.get("inclination_deg"), 1), fnum(r.get("period_min"), 1),
                conf_cell(r.get("confidence"), r.get("confidence_basis", "")), rf,
            ])
        more = len(recs) - rows_per_launch
        note = f"+ {more} {'more ' if rows else ''}from this launch in new_objects.csv" if more > 0 else ""
        body = (table(["NORAD", "Name", "Type", "Orbit", "Perigee × apogee", "Incl °", "Period min",
                       "Confidence", "Radio (SatNOGS)"], rows, note) if rows
                else f'<div style="font-size:12px;color:{MUTED}">{note}</div>')
        out.append(f'<div style="font-size:13px;color:{TEXT};margin:14px 0 6px">{head}</div>' + body)
    if len(groups) > max_launches:
        out.append(f'<div style="font-size:12px;color:{MUTED};margin-top:8px">'
                   f'+ {len(groups) - max_launches} more launches in new_objects.csv</div>')
    return "".join(out)


def removed_html(gone: list[dict], limit: int = MAX_REMOVED_ROWS) -> str:
    rows = []
    for r in gone[:limit]:
        when = r.get("decay_epoch") or r.get("decay_date") or "–"
        rows.append([e(r["norad"]), e(r.get("name")), e((r.get("type") or "").title()),
                     e(r.get("country") or r.get("country_code")), e(r.get("launch_date")),
                     e(when), fnum(r.get("days_in_orbit")), e(r.get("removal_reason")),
                     conf_cell(r.get("confidence"), r.get("confidence_basis", ""))])
    more = len(gone) - limit
    return table(["NORAD", "Name", "Type", "Country", "Launched", "Re-entry", "Days up", "Reason",
                  "Confidence"], rows,
                 f"+ {more} more in removed_objects.csv" if more > 0 else "")


# -- reports -----------------------------------------------------------------
def build_baseline(res: RunResult) -> Report:
    objs = res.objects
    tc = type_counts(objs)
    date = res.run_at.date().isoformat()
    total = len(objs)
    recent_cut = (res.run_at - timedelta(days=30)).date().isoformat()
    recent = [r for r in objs.values() if (r.get("launch_date") or "") >= recent_cut]
    by_day = Counter(r["launch_date"] for r in recent)
    sections = [
        section("Catalog baseline", kpi_row([
            ("Objects in orbit", f"{total:,}", "tracked & public"),
            ("Payloads", f"{tc.get('PAYLOAD', 0):,}", "satellites & spacecraft"),
            ("Rocket bodies", f"{tc.get('ROCKET BODY', 0):,}", "spent stages"),
            ("Debris", f"{tc.get('DEBRIS', 0):,}", f"+ {tc.get('TBA', 0) + tc.get('UNKNOWN', 0):,} unidentified"),
        ]), "This snapshot is the starting point. From tomorrow, each daily report lists what was "
            "added to and removed from orbit since the previous run."),
        section("What is up there", composition(objs)),
        section("Launched in the last 30 days, still in orbit",
                hbar("Objects by launch date", sorted(by_day.items(), reverse=True), NEW, top=31, by_value=False),
                f"{len(recent):,} objects"),
    ]
    if res.notes:
        sections.append(section("Notes", "<br>".join(e(n) for n in res.notes)))
    html = wrap(f"Baseline · {date}", f"{total:,} objects currently in orbit", sections, FOOTER)
    text = (f"Gonk orbital report — baseline {date}\n\n"
            f"Objects in orbit: {total:,}\n  Payloads: {tc.get('PAYLOAD', 0):,}\n"
            f"  Rocket bodies: {tc.get('ROCKET BODY', 0):,}\n  Debris: {tc.get('DEBRIS', 0):,}\n\n"
            "Daily reports of new and removed objects start with the next run.\n")
    return Report(subject=f"Orbit baseline {date}: {total:,} objects in orbit", html=html, text=text,
                  attachments={"catalog.zip": zipped("catalog.csv", to_csv(sorted(objs.values(), key=lambda r: r["norad"])))})


def build_daily(res: RunResult) -> Report:
    date = res.run_at.date().isoformat()
    total = len(res.objects)
    delta = total - (res.prev_total or total)
    new_in_orbit = [r for r in res.new if not r.get("decay_date")]
    new_pay = [r for r in new_in_orbit if r.get("type") == "PAYLOAD"]
    since = res.since[:16].replace("T", " ") + " UTC" if res.since else "previous run"
    launches = len({r.get("launch_id") for r in res.new})

    sections = [section("", kpi_row([
        ("In orbit", f"{total:,}", f"{delta:+,} since last run"),
        ("New objects", f"{len(res.new):,}", f"{len(new_pay):,} payloads · {launches} launch{'es' if launches != 1 else ''}"),
        ("Removed", f"{len(res.gone):,}", f"{sum(1 for g in res.gone if g.get('decay_date')):,} re-entered"),
        ("Renamed / identified", f"{len(res.renamed):,}", "catalog updates"),
    ]))]

    if res.new:
        by_country = Counter(r.get("country") or r.get("country_code") or "Unknown" for r in res.new)
        by_type = Counter((r.get("type") or "Unknown").title() for r in res.new)
        by_regime = Counter(r.get("regime") or "Unknown" for r in new_in_orbit)
        by_const = Counter(r.get("constellation") or "Other / independent" for r in new_pay)
        charts = (two_up(hbar("By country", list(by_country.items()), NEW),
                         hbar("By object type", list(by_type.items()), NEW))
                  + '<div style="height:16px"></div>'
                  + two_up(hbar("By orbit", list(by_regime.items()), NEW),
                           hbar("Payloads by constellation", list(by_const.items()), NEW)))
        sections.append(section("New in orbit", charts + NEW_SLOT,
                                f"{len(res.new):,} objects added to the catalog since {e(since)}, grouped by launch"
                                f"<br>Confidence: {conf_counts(res.new)}"))
    else:
        sections.append(section("New in orbit", f'<div style="font-size:13px;color:{TEXT2}">'
                                                f'No objects were added to the catalog since {e(since)}.</div>'))

    if res.gone:
        by_type = Counter((r.get("type") or "Unknown").title() for r in res.gone)
        by_country = Counter(r.get("country") or r.get("country_code") or "Unknown" for r in res.gone)
        sections.append(section(
            "No longer in orbit",
            two_up(hbar("By object type", list(by_type.items()), GONE),
                   hbar("By country", list(by_country.items()), GONE))
            + '<div style="height:12px"></div>' + GONE_SLOT,
            f"{len(res.gone):,} objects re-entered or left the public catalog"))
    else:
        sections.append(section("No longer in orbit", f'<div style="font-size:13px;color:{TEXT2}">'
                                                      f'No objects were removed since {e(since)}.</div>'))

    if res.renamed:
        rows = []
        for rn in res.renamed[:40]:
            r = rn["record"]
            what = "; ".join(f"{k}: {e(a)} → <b>{e(b)}</b>" for k, (a, b) in rn["changes"].items())
            rows.append([e(rn["norad"]), e(r.get("intldes")), what])
        sections.append(section("Renamed or newly identified", table(["NORAD", "Int'l designator", "Change"], rows),
                                "Placeholder names such as “OBJECT A” are replaced once an object is identified"))

    if res.orbit_updates:
        rows = [[e(r["norad"]), e(r.get("name")), e(r.get("added")), e(orbit_text(r)), e(alt_text(r)),
                 fnum(r.get("inclination_deg"), 1), conf_cell(r.get("confidence"), r.get("confidence_basis", ""))]
                for r in res.orbit_updates[:25]]
        more = len(res.orbit_updates) - 25
        sections.append(section(
            "Orbits now published",
            table(["NORAD", "Name", "First reported", "Orbit", "Perigee × apogee", "Incl °", "Confidence"], rows,
                  f"+ {more} more in orbit_updates.csv" if more > 0 else ""),
            "Objects from earlier reports whose first orbital elements arrived since the last run"))

    if res.predictions:
        rows = [[e(p["norad"]), e(p.get("name")), e((p.get("type") or "").title()),
                 e(p.get("country") or p.get("country_code")), e(p["predicted_epoch"]),
                 e({"TIP_msg": "TIP message", "60day_msg": "60-day forecast"}.get(
                     p.get("prediction_source"), p.get("prediction_source"))),
                 conf_cell(p.get("confidence"), p.get("confidence_basis", ""))]
                for p in res.predictions[:30]]
        sections.append(section("Predicted re-entries (next 7 days)",
                                table(["NORAD", "Name", "Type", "Country", "Predicted (UTC)", "Source",
                                       "Confidence"], rows)))

    trend = trend_chart(res.history)
    if trend:
        sections.append(section("Last 30 days", trend))
    sections.append(section("Catalog at a glance", composition(res.objects)))
    sections.append(section("How confident is this?", method_html(),
                            "Each object is rated by its weakest catalog or orbit fact. Radio frequencies "
                            "and constellations are rated separately because they come from a community "
                            "database or from name matching."))
    notes = list(res.notes)
    if res.full_sync:
        notes.append("Full catalog re-sync performed this run (weekly check for anything the daily feeds missed).")
    if notes:
        sections.append(section("Notes", f'<div style="font-size:12px;color:{TEXT2}">'
                                         + "<br>".join(e(n) for n in notes) + "</div>"))

    subtitle = f"Changes since {e(since)} · {total:,} objects in orbit"
    template = wrap(f"Daily · {date}", subtitle, sections, FOOTER)
    # Gmail clips messages over ~102 KB, so shrink the per-object tables until it fits.
    # Every object is always in the attached CSVs.
    for rows, launches, removed in SIZE_STEPS:
        html = (template.replace(NEW_SLOT, new_objects_html(res.new, res.run_at, rows, launches))
                        .replace(GONE_SLOT, removed_html(res.gone, removed)))
        if len(html.encode("utf-8")) <= MAX_HTML_BYTES:
            break

    lines = [f"Gonk orbital report — {date}", f"Changes since {since}", "",
             f"In orbit: {total:,} ({delta:+,})", f"New: {len(res.new):,} ({len(new_pay):,} payloads)",
             f"Removed: {len(res.gone):,}", ""]
    for lid, recs in launch_groups(res.new)[:MAX_LAUNCHES]:
        f0 = recs[0]
        lines.append(f"{lid}  launched {f0.get('launch_date')}  {f0.get('country') or f0.get('country_code')}  "
                     f"{len(recs)} object(s)")
        for r in recs[:ROWS_PER_LAUNCH]:
            lines.append(f"   {r['norad']:>6}  {r.get('name', ''):<28} {orbit_text(r):<16} {alt_text(r)}")
    if res.gone:
        lines += ["", "No longer in orbit:"]
        lines += [f"   {r['norad']:>6}  {r.get('name', ''):<28} {r.get('removal_reason')}" for r in res.gone[:MAX_REMOVED_ROWS]]
    text = "\n".join(lines) + "\n\nFull details are in the attached CSV files.\n"

    attachments = {}
    if res.new:
        attachments["new_objects.csv"] = to_csv(res.new)
    if res.gone:
        attachments["removed_objects.csv"] = to_csv(res.gone)
    if res.predictions:
        attachments["predicted_reentries.csv"] = to_csv(res.predictions)
    if res.orbit_updates:
        attachments["orbit_updates.csv"] = to_csv(res.orbit_updates)
    subject = f"Orbit report {date}: {len(res.new):,} new, {len(res.gone):,} removed ({total:,} in orbit)"
    return Report(subject=subject, html=html, text=text, attachments=attachments)


def build_failure(kind: str, when: datetime, error: BaseException) -> Report:
    """Short email sent when a scheduled run fails, so recipients aren't left waiting."""
    date = when.date().isoformat()
    msg = f"{type(error).__name__}: {error}"
    hint = ("Check the Space-Track login with 'gonk setup', and 'gonk rules' for query limits. "
            "The next scheduled run will try again; nothing is lost, because the next run covers "
            "everything since the last successful one.")
    text = f"Gonk could not build the {kind} orbit report for {date}.\n\n{msg}\n\n{hint}\n"
    html = wrap(f"Report failed · {date}", f"The {e(kind)} run did not complete",
                [section("What happened", f'<pre style="white-space:pre-wrap;font-size:12px;color:{TEXT}">{e(msg)}</pre>'
                         f'<div style="font-size:13px;color:{TEXT2}">{e(hint)}</div>')], FOOTER)
    return Report(subject=f"Orbit report {date}: run failed", html=html, text=text)


def build(res: RunResult) -> Report:
    return build_baseline(res) if res.kind == "baseline" else build_daily(res)
