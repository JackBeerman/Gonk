"""Source attribution and confidence ratings for every reported fact.

Each reported record gets a `prov` dict: {field group: {source, confidence, basis}}.
Confidence is one of HIGH / MEDIUM / LOW, and `basis` is a short, plain-language
reason. The rules are intentionally simple and conservative, so a reader can check
them against the "How confident is this?" section of the report.

Field groups
  name, country, type   - catalog identity (Space-Track SATCAT, 18th/19th SDS)
  orbit                 - perigee/apogee/inclination/period (Space-Track GP elements)
  regime                - derived by Gonk from the orbit; inherits the orbit's rating
  constellation         - inferred by Gonk from the object's name (never above MEDIUM)
  rf                    - transmitter frequencies (SatNOGS DB, community-contributed)
  removal, prediction   - re-entry facts and forecasts (Space-Track decay data)

A record's overall rating is the lowest of its catalog and orbit facts (name, country,
type, orbit). Inferred and community facts (constellation, rf) carry their own
ratings and do not lower the overall rating, because they are shown separately.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

HIGH, MEDIUM, LOW = "high", "medium", "low"
RANK = {HIGH: 2, MEDIUM: 1, LOW: 0}

SPACETRACK_SATCAT = "Space-Track SATCAT"
SPACETRACK_GP = "Space-Track GP elements"
SPACETRACK_DECAY = "Space-Track decay messages"
GONK_DERIVED = "Derived by Gonk"
SATNOGS = "SatNOGS DB (community)"

OVERALL_GROUPS = ("name", "country", "type", "orbit")
# Every rated field group, in CSV column order.
RATED_GROUPS = ("name", "country", "type", "orbit", "regime", "constellation", "rf", "removal", "prediction")

PLACEHOLDER_NAME = re.compile(r"^(OBJECT [A-Z]{1,3}|TBA.*|UNKNOWN.*)$")
UNKNOWN_COUNTRIES = {"TBD", "UNK", ""}
UNKNOWN_TYPES = {"TBA", "UNKNOWN", ""}

# Public explanation of the rules, rendered in the report and README.
METHOD = [
    (SPACETRACK_SATCAT, "Name, country, object type, launch date and site",
     "High: the official U.S. Space Force catalog. Medium: placeholder names such as “OBJECT A” "
     "(identification pending). Low: country listed as TBD or object type TBA/unknown."),
    (SPACETRACK_GP, "Orbit: perigee, apogee, inclination, period",
     "High: elements under 2 days old for an object tracked 3+ days. Medium: elements 2–7 days old, "
     "or from the first days after launch, when objects are still separating or maneuvering. "
     "Low: elements older than 7 days, or no elements published yet."),
    (GONK_DERIVED, "Orbit class, sun-synchronous flag, constellation",
     "Orbit class has the same rating as the orbit it is computed from. Constellation is matched "
     "from the object name, so it is at most Medium."),
    (SPACETRACK_DECAY, "Re-entries and re-entry forecasts",
     "High: re-entry recorded in the catalog. Low: object left the public catalog with no decay "
     "reported. Forecasts: Medium for TIP messages (issued in the final days), Low for 60-day forecasts."),
    (SATNOGS, "Radio frequencies",
     "Community-contributed. High: confirmed and IARU-coordinated or ITU-notified. Medium: confirmed "
     "but not coordinated. Low: unconfirmed, flagged as a frequency violation, or listed for an object "
     "whose identity is still provisional (the frequency may belong to a sibling object)."),
]


def fact(source: str, confidence: str, basis: str) -> dict:
    return {"source": source, "confidence": confidence, "basis": basis}


LABELS = {"name": "Name", "country": "Country", "type": "Type", "orbit": "Orbit",
          "removal": "Re-entry", "prediction": "Forecast"}


def rate(rec: dict, groups: tuple[str, ...]) -> None:
    """Set the overall rating from the weakest of `groups` (first one wins on ties)."""
    prov = rec["prov"]
    group = min((g for g in groups if g in prov), key=lambda g: RANK[prov[g]["confidence"]])
    f = prov[group]
    rec["confidence"] = f["confidence"]
    rec["confidence_basis"] = "" if f["confidence"] == HIGH else f"{LABELS[group]}: {f['basis']}"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def provisional_identity(rec: dict) -> bool:
    return bool(PLACEHOLDER_NAME.match((rec.get("name") or "").upper()))


def assess_identity(rec: dict) -> dict[str, dict]:
    out = {}
    if provisional_identity(rec):
        out["name"] = fact(SPACETRACK_SATCAT, MEDIUM, "placeholder name; identification pending")
    else:
        out["name"] = fact(SPACETRACK_SATCAT, HIGH, "official catalog name")
    cc = (rec.get("country_code") or "").upper()
    if cc in UNKNOWN_COUNTRIES:
        out["country"] = fact(SPACETRACK_SATCAT, LOW, "owner not yet assigned (TBD)")
    else:
        out["country"] = fact(SPACETRACK_SATCAT, HIGH, "official catalog owner")
    otype = (rec.get("type") or "").upper()
    if otype in UNKNOWN_TYPES:
        out["type"] = fact(SPACETRACK_SATCAT, LOW, "object type not yet determined")
    else:
        out["type"] = fact(SPACETRACK_SATCAT, HIGH, "official catalog type")
    return out


def assess_orbit(rec: dict, now: datetime) -> dict:
    if rec.get("perigee_km") is None or rec.get("apogee_km") is None:
        return fact(SPACETRACK_GP, LOW, "no orbit published yet")
    epoch = _parse(rec.get("epoch"))
    if epoch is None:
        return fact(SPACETRACK_SATCAT, MEDIUM, "catalog summary values; no current element set")
    age = (now - epoch).total_seconds() / 86400
    launch = _parse(rec.get("launch_date"))
    tracked = (epoch - launch).total_seconds() / 86400 if launch else None
    if age > 7:
        return fact(SPACETRACK_GP, LOW, f"elements {age:.0f} days old")
    if age > 2:
        return fact(SPACETRACK_GP, MEDIUM, f"elements {age:.1f} days old")
    if tracked is not None and tracked < 3:
        return fact(SPACETRACK_GP, MEDIUM, f"early elements, {max(tracked, 0):.1f} days after launch")
    return fact(SPACETRACK_GP, HIGH, f"elements {age:.1f} days old")


def assess_rf(tx: dict, rec: dict) -> dict:
    """Rate one SatNOGS transmitter entry for this record."""
    if provisional_identity(rec):
        return fact(SATNOGS, LOW, "object identity provisional; may belong to a sibling object")
    if tx.get("unconfirmed"):
        return fact(SATNOGS, LOW, "unconfirmed entry")
    if tx.get("frequency_violation"):
        return fact(SATNOGS, LOW, "flagged as a frequency violation")
    coordinated = (tx.get("iaru_coordination") or "").lower().startswith("iaru coordinated")
    if coordinated or tx.get("itu_notification"):
        return fact(SATNOGS, HIGH, "confirmed; IARU-coordinated or ITU-notified")
    return fact(SATNOGS, MEDIUM, "confirmed by SatNOGS; not coordinated")


def assess_new(rec: dict, now: datetime) -> None:
    """Attach `prov`, overall `confidence` and `confidence_basis` to a new or reported record."""
    prov = assess_identity(rec)
    prov["orbit"] = assess_orbit(rec, now)
    orbit = prov["orbit"]
    prov["regime"] = fact(GONK_DERIVED, orbit["confidence"], f"computed from orbit ({orbit['basis']})")
    if rec.get("constellation"):
        prov["constellation"] = fact(GONK_DERIVED, MEDIUM, "matched from object name")
    if rec.get("rf"):
        best = max((t["confidence"] for t in rec["rf"]), key=RANK.get)
        prov["rf"] = fact(SATNOGS, best, f"{len(rec['rf'])} transmitter entr{'y' if len(rec['rf']) == 1 else 'ies'}")
    rec["prov"] = prov
    rate(rec, OVERALL_GROUPS)


def assess_removal(rec: dict) -> None:
    prov = assess_identity(rec)
    if rec.get("decay_date"):
        basis = "re-entry recorded in catalog"
        if rec.get("decay_epoch"):
            basis += f"; epoch from {rec.get('decay_source') or 'decay message'}"
        prov["removal"] = fact(SPACETRACK_DECAY, HIGH, basis)
    else:
        prov["removal"] = fact(SPACETRACK_SATCAT, LOW, "left the public catalog; reason not published")
    rec["prov"] = prov
    rate(rec, ("removal", "name", "type"))


def assess_prediction(p: dict) -> None:
    src = p.get("prediction_source")
    if src == "TIP_msg":
        f = fact(SPACETRACK_DECAY, MEDIUM, "TIP forecast; window narrows as re-entry approaches")
    else:
        f = fact(SPACETRACK_DECAY, LOW, "60-day forecast; can shift by days")
    p["prov"] = {"prediction": f}
    rate(p, ("prediction",))


def flatten(rec: dict) -> dict:
    """CSV columns: <group>_confidence, <group>_source, <group>_basis for each rated group."""
    out = {}
    for group, f in (rec.get("prov") or {}).items():
        out[f"{group}_confidence"] = f["confidence"]
        out[f"{group}_source"] = f["source"]
        out[f"{group}_basis"] = f["basis"]
    return out
