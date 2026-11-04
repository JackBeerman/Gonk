"""Baseline and daily-diff logic.

Baseline (first run): download the full on-orbit SATCAT once and store it.

Daily run (once per day, after 17:00 UTC, when Space-Track publishes catalog updates):
  * satcat_debut  - objects added to the catalog since the last run      -> "new"
  * satcat_change - catalog edits since the last run: decays, renames    -> "removed", "renamed"
  * decay         - decay messages: exact re-entry epochs + predictions
  * gp            - current orbital elements (and TLE) for the new objects only
  * once a week the full on-orbit SATCAT is re-downloaded and diffed against the
    snapshot to catch anything the incremental feeds did not report.
Queries use a one-day overlap with the previous run and are de-duplicated against
the snapshot, so a late or missed run never loses or double-counts objects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import catalog as cat
from . import confidence as conf
from . import satnogs
from .spacetrack import PolicyError, SpaceTrack
from .store import Store, age_days

log = logging.getLogger(__name__)

FULL_SYNC_DAYS = 7
REFS_MAX_AGE_DAYS = 7
PREDICTION_WINDOW_DAYS = 7
REPORT_ONLY = {"prov", "confidence", "confidence_basis", "rf"}


@dataclass
class RunResult:
    kind: str                       # "baseline" | "daily"
    run_at: datetime
    since: str | None = None        # previous run timestamp (daily)
    new: list[dict] = field(default_factory=list)
    gone: list[dict] = field(default_factory=list)
    renamed: list[dict] = field(default_factory=list)
    predictions: list[dict] = field(default_factory=list)
    objects: dict[int, dict] = field(default_factory=dict)
    prev_total: int | None = None
    full_sync: bool = False
    history: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _title(name: str) -> str:
    small = {"Of", "And", "The", "For"}
    words = [w.capitalize() for w in name.lower().split()]
    return " ".join(w.lower() if i and w in small else w for i, w in enumerate(words))


def refresh_refs(st: SpaceTrack | None, store: Store, now: datetime, notes: list[str]) -> dict:
    refs = store.load_refs()
    if st is not None and age_days(refs.get("fetched"), now) >= REFS_MAX_AGE_DAYS:
        try:
            box = st.fetch("boxscore")
            sites = st.fetch("launch_site")
            refs = {
                "fetched": now.isoformat(timespec="seconds"),
                "countries": {r["SPADOC_CD"]: _title(r["COUNTRY"]) for r in box if r.get("SPADOC_CD")},
                "sites": {r["SITE_CODE"]: _title(r["LAUNCH_SITE"]) for r in sites if r.get("SITE_CODE")},
            }
            store.save_refs(refs)
        except PolicyError as e:
            notes.append(f"Reference tables not refreshed: {e}")
    refs.setdefault("countries", {})
    refs.setdefault("sites", {})
    return refs


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _enrich_new(st: SpaceTrack, store: Store, recs: list[dict], refs: dict,
                now: datetime, notes: list[str]) -> None:
    in_orbit = [r["norad"] for r in recs if not r.get("decay_date")]
    if in_orbit:
        try:
            gp_rows = st.fetch("gp", ids=in_orbit)
            by_id = {int(g["NORAD_CAT_ID"]): g for g in gp_rows}
            for r in recs:
                if r["norad"] in by_id:
                    cat.apply_gp(r, by_id[r["norad"]])
        except PolicyError as e:
            notes.append(f"Orbital elements not fetched: {e}")
    rf = satnogs.load_transmitters(store.root, now) if recs else {}
    for r in recs:
        cat.apply_refs(r, refs["countries"], refs["sites"])
        entries = [{"text": satnogs.summarize(tx), **conf.assess_rf(tx, r)} for tx in rf.get(r["norad"], [])]
        if entries:
            r["rf"] = sorted(entries, key=lambda x: -conf.RANK[x["confidence"]])
        conf.assess_new(r, now)


def run_baseline(st: SpaceTrack | None, store: Store, now: datetime,
                 rows: list[dict] | None = None) -> RunResult:
    """Download (or import from `rows`) the full on-orbit catalog and store it."""
    res = RunResult(kind="baseline", run_at=now)
    if rows is None:
        assert st is not None
        rows = st.fetch("satcat", ["CURRENT/Y", "DECAY/null-val"],
                        orderby="NORAD_CAT_ID", predicates=cat.SATCAT_PREDICATES)
    if not rows:
        raise RuntimeError("Space-Track returned an empty catalog; not saving a baseline.")
    refs = refresh_refs(st, store, now, res.notes)
    objects = {}
    for r in rows:
        rec = cat.from_satcat(r)
        if rec.get("decay_date"):
            continue
        cat.apply_refs(rec, refs["countries"], refs["sites"])
        objects[rec["norad"]] = rec
    ts = now.isoformat(timespec="seconds")
    meta = {"baseline_at": ts, "last_run": ts, "last_full_sync": ts}
    store.save_catalog(meta, objects)
    store.append_history({"date": now.date().isoformat(), "run_at": ts, "kind": "baseline",
                          "total": len(objects), "new": 0, "gone": 0})
    res.objects = objects
    res.full_sync = True
    res.history = store.load_history()
    return res


def run_daily(st: SpaceTrack, store: Store, now: datetime, force_full: bool = False) -> RunResult:
    meta, objects = store.load_catalog()
    res = RunResult(kind="daily", run_at=now, since=meta.get("last_run"), prev_total=len(objects))
    since_date = (datetime.fromisoformat(meta["last_run"]) - timedelta(days=1)).date().isoformat()
    refs = refresh_refs(st, store, now, res.notes)

    changes = st.fetch("satcat_change", [f"CHANGE_MADE/>{since_date}"], orderby="CHANGE_MADE asc")
    decays = st.fetch("decay", [f"MSG_EPOCH/>{since_date}"], orderby="MSG_EPOCH asc")

    full = force_full or age_days(meta.get("last_full_sync"), now) >= FULL_SYNC_DAYS
    res.full_sync = full
    if full:
        rows = st.fetch("satcat", ["CURRENT/Y", "DECAY/null-val"],
                        orderby="NORAD_CAT_ID", predicates=cat.SATCAT_PREDICATES)
        if not rows:
            raise RuntimeError("Space-Track returned an empty catalog; aborting this run.")
        current: dict[int, dict] = {}
        for r in rows:
            rec = cat.from_satcat(r)
            current[rec["norad"]] = rec
        new_recs = [current[i] for i in sorted(set(current) - set(objects))]
        gone_ids = set(objects) - set(current)
    else:
        debut = st.fetch("satcat_debut", [f"DEBUT/>{since_date}"], orderby="NORAD_CAT_ID")
        new_recs = [cat.from_satcat(r) for r in debut if int(r["NORAD_CAT_ID"]) not in objects]
        gone_ids = {int(c["NORAD_CAT_ID"]) for c in changes
                    if c.get("CURRENT_DECAY") and int(c["NORAD_CAT_ID"]) in objects}

    # De-duplicate (an id can appear in several debut rows across the overlap window), and
    # skip objects that left orbit recently: the overlap window would otherwise re-report them.
    departed: dict[str, str] = meta.get("departed", {})
    new_recs = [r for r in {r["norad"]: r for r in new_recs}.values() if str(r["norad"]) not in departed]
    _enrich_new(st, store, new_recs, refs, now, res.notes)

    # -- removed objects ---------------------------------------------------
    decay_by_id: dict[int, dict] = {}
    for d in decays:
        if d.get("MSG_TYPE") == "Historical":
            decay_by_id[int(d["NORAD_CAT_ID"])] = d
    change_by_id = {int(c["NORAD_CAT_ID"]): c for c in changes}
    for norad in sorted(gone_ids):
        rec = dict(objects[norad])
        ch, dm = change_by_id.get(norad), decay_by_id.get(norad)
        if ch and ch.get("CURRENT_DECAY"):
            rec["decay_date"] = ch["CURRENT_DECAY"]
        if dm:
            rec["decay_epoch"] = dm.get("DECAY_EPOCH")
            rec["decay_source"] = dm.get("SOURCE")
        rec["removal_reason"] = ("Re-entered (decayed)" if rec.get("decay_date")
                                 else "Removed from public catalog (no decay reported)")
        launch, end = _parse_ts(rec.get("launch_date")), _parse_ts(rec.get("decay_date"))
        if launch and end:
            rec["days_in_orbit"] = (end - launch).days
        conf.assess_removal(rec)
        res.gone.append(rec)

    # -- renames / country changes -----------------------------------------
    for c in changes:
        norad = int(c["NORAD_CAT_ID"])
        rec = objects.get(norad)
        if not rec or norad in gone_ids:
            continue
        new_name, new_cc = c.get("CURRENT_NAME"), c.get("CURRENT_COUNTRY")
        diff = {}
        if c.get("PREVIOUS_NAME") and new_name and new_name != rec.get("name"):
            diff["name"] = (rec.get("name"), new_name)
            rec["name"] = new_name
            rec["constellation"] = cat.constellation(new_name)
        if c.get("PREVIOUS_COUNTRY") and new_cc and new_cc != rec.get("country_code"):
            diff["country"] = (rec.get("country") or rec.get("country_code"),
                               refs["countries"].get(new_cc, new_cc))
            rec["country_code"] = new_cc
            cat.apply_refs(rec, refs["countries"], refs["sites"])
        if diff:
            res.renamed.append({"norad": norad, "changes": diff, "record": rec})

    # -- re-entry predictions for objects still in orbit --------------------
    horizon = now + timedelta(days=PREDICTION_WINDOW_DAYS)
    latest: dict[int, dict] = {}
    for d in decays:
        norad = int(d["NORAD_CAT_ID"])
        if d.get("MSG_TYPE") != "Prediction" or norad in gone_ids or norad not in objects:
            continue
        epoch = _parse_ts(d.get("DECAY_EPOCH"))
        if epoch and now <= epoch <= horizon:
            prev = latest.get(norad)
            if not prev or (d.get("MSG_EPOCH") or "") >= (prev.get("MSG_EPOCH") or ""):
                latest[norad] = d
    for norad, d in sorted(latest.items(), key=lambda kv: kv[1]["DECAY_EPOCH"]):
        rec = objects[norad]
        pred = {**rec, "predicted_epoch": d["DECAY_EPOCH"], "prediction_source": d.get("SOURCE")}
        conf.assess_prediction(pred)
        res.predictions.append(pred)

    # -- update snapshot -----------------------------------------------------
    today = now.date().isoformat()
    for norad in gone_ids:
        objects.pop(norad, None)
        departed[str(norad)] = today
    for r in new_recs:
        if r.get("decay_date"):
            departed[str(r["norad"])] = today   # appeared and re-entered between runs
        else:  # report-only fields (ratings, radio lookups) are not kept in the snapshot
            objects[r["norad"]] = {k: v for k, v in r.items() if k not in REPORT_ONLY}
    cutoff = (now - timedelta(days=30)).date().isoformat()
    meta["departed"] = {k: v for k, v in departed.items() if v >= cutoff}
    res.new = sorted(new_recs, key=lambda r: (r.get("launch_date") or "", r["norad"]), reverse=True)
    ts = now.isoformat(timespec="seconds")
    meta["last_run"] = ts
    if full:
        meta["last_full_sync"] = ts
    store.save_catalog(meta, objects)
    store.append_history({
        "date": now.date().isoformat(), "run_at": ts, "kind": "daily", "total": len(objects),
        "new": len(res.new), "gone": len(res.gone), "renamed": len(res.renamed),
        "new_ids": [r["norad"] for r in res.new], "gone_ids": sorted(gone_ids),
    })
    res.objects = objects
    res.history = store.load_history()
    return res
