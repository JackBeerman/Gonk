"""Normalize Space-Track records into one satellite record shape, and enrich them."""

from __future__ import annotations

import math
import re
from typing import Any

EARTH_RADIUS_KM = 6378.137
GEO_PERIOD_MIN = 1436.07

# Fields kept from the SATCAT for the stored snapshot (keeps baseline downloads small).
SATCAT_PREDICATES = [
    "NORAD_CAT_ID", "OBJECT_ID", "OBJECT_NAME", "OBJECT_TYPE", "COUNTRY", "LAUNCH",
    "SITE", "DECAY", "PERIOD", "INCLINATION", "APOGEE", "PERIGEE", "RCS_SIZE", "COMMENT",
]

RECORD_FIELDS = [
    "norad", "name", "intldes", "launch_id", "type", "country_code", "country",
    "launch_date", "site_code", "site", "decay_date", "period_min", "revs_per_day",
    "inclination_deg", "apogee_km", "perigee_km", "eccentricity", "semimajor_axis_km",
    "regime", "sso", "constellation", "rcs_size", "epoch", "bstar", "debut",
    "comment", "rf", "tle1", "tle2",
]

# Known constellations / programs, matched against the object name.
CONSTELLATIONS = [
    (r"^STARLINK", "Starlink (SpaceX)"),
    (r"^ONEWEB", "OneWeb (Eutelsat)"),
    (r"^KUIPER", "Project Kuiper (Amazon)"),
    (r"^QIANFAN|^QIANFAN|^G60", "Qianfan / Thousand Sails (China)"),
    (r"^HULIANWANG", "Guowang / SatNet (China)"),
    (r"^IRIDIUM", "Iridium"),
    (r"^GLOBALSTAR", "Globalstar"),
    (r"^ORBCOMM", "ORBCOMM"),
    (r"^O3B", "O3b mPOWER (SES)"),
    (r"^FLOCK|^DOVE|^SUPERDOVE|^PELICAN", "Planet"),
    (r"^SKYSAT", "Planet SkySat"),
    (r"^LEMUR", "Spire"),
    (r"^HAWK", "HawkEye 360"),
    (r"^ICEYE", "ICEYE"),
    (r"^CAPELLA", "Capella Space"),
    (r"^UMBRA", "Umbra"),
    (r"^QPS-SAR", "iQPS"),
    (r"^BLACKSKY|^GLOBAL-", "BlackSky"),
    (r"^WORLDVIEW|^LEGION", "Maxar"),
    (r"^NAVSTAR|^GPS ", "GPS (US)"),
    (r"^GLONASS|^COSMOS 2[45]\d\d.*GLONASS", "GLONASS (Russia)"),
    (r"^BEIDOU", "BeiDou (China)"),
    (r"^GSAT0|^GALILEO", "Galileo (EU)"),
    (r"^NVS-|^IRNSS", "NavIC (India)"),
    (r"^YAOGAN", "Yaogan (China)"),
    (r"^JILIN", "Jilin-1 (China)"),
    (r"^TIANQI", "Tianqi (China)"),
    (r"^GEESAT|^GEELY", "Geely (China)"),
    (r"^SHIYAN", "Shiyan (China)"),
    (r"^TRANCHE|^T1TL|^T1TR|^T2TL", "SDA PWSA (US)"),
    (r"^ISS|^ZARYA", "International Space Station"),
    (r"^TIANHE|^CSS ", "Tiangong (China)"),
]
_CONSTELLATION_RE = [(re.compile(p), n) for p, n in CONSTELLATIONS]


def _num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    return None if v in (None, "") else str(v)


def launch_id(intldes: str | None) -> str | None:
    """'2026-246Z' -> '2026-246' (all pieces of one launch share this)."""
    if not intldes:
        return None
    m = re.match(r"^(\d{4}-\d{3})", intldes)
    return m.group(1) if m else None


def constellation(name: str | None) -> str | None:
    if not name:
        return None
    up = name.upper()
    for rx, label in _CONSTELLATION_RE:
        if rx.search(up):
            return label
    return None


def sso_inclination(semimajor_km: float, ecc: float = 0.0) -> float | None:
    """Inclination (deg) for a sun-synchronous orbit at this size, or None if impossible."""
    j2, mu = 1.08262668e-3, 398600.4418
    omega_dot = 2 * math.pi / (365.2422 * 86400)  # required nodal precession, rad/s
    n = math.sqrt(mu / semimajor_km ** 3)
    p = semimajor_km * (1 - ecc ** 2)
    cos_i = -omega_dot / (1.5 * n * j2 * (EARTH_RADIUS_KM / p) ** 2)
    if abs(cos_i) > 1:
        return None
    return math.degrees(math.acos(cos_i))


def classify_orbit(rec: dict) -> None:
    """Fill derived orbit fields (eccentricity, regime, SSO flag, revs/day) in place."""
    per, apo, peri, inc = rec.get("period_min"), rec.get("apogee_km"), rec.get("perigee_km"), rec.get("inclination_deg")
    if per:
        rec["revs_per_day"] = round(1440.0 / per, 4)
    if apo is not None and peri is not None:
        ra, rp = apo + EARTH_RADIUS_KM, peri + EARTH_RADIUS_KM
        if rec.get("eccentricity") is None:
            rec["eccentricity"] = round((ra - rp) / (ra + rp), 6)
        if rec.get("semimajor_axis_km") is None:
            rec["semimajor_axis_km"] = round((ra + rp) / 2, 1)
    ecc, a = rec.get("eccentricity"), rec.get("semimajor_axis_km")
    if apo is None or peri is None or ecc is None:
        rec["regime"] = "Unknown"
        rec["sso"] = False
        return
    if ecc >= 0.25:
        regime = "GTO" if peri < 2000 and 33000 < apo < 37500 else "HEO"
    elif apo < 2000:
        regime = "LEO"
    elif per and abs(per - GEO_PERIOD_MIN) < 30 and ecc < 0.05:
        regime = "GEO" if (inc or 0) < 5 else "GSO (inclined)"
    elif peri >= 2000 and apo < 34000:
        regime = "MEO"
    elif peri > 36500:
        regime = "Beyond GEO"
    else:
        regime = "Other"
    rec["regime"] = regime
    sso = False
    if regime == "LEO" and inc is not None and a:
        target = sso_inclination(a, ecc)
        sso = target is not None and abs(inc - target) <= 1.0
    rec["sso"] = sso


def from_satcat(r: dict) -> dict:
    """Normalize a satcat / satcat_debut row."""
    rec = {
        "norad": int(r["NORAD_CAT_ID"]),
        "name": _str(r.get("OBJECT_NAME") or r.get("SATNAME")),
        "intldes": _str(r.get("OBJECT_ID") or r.get("INTLDES")),
        "type": _str(r.get("OBJECT_TYPE")) or "UNKNOWN",
        "country_code": _str(r.get("COUNTRY")),
        "launch_date": _str(r.get("LAUNCH")),
        "site_code": _str(r.get("SITE")),
        "decay_date": _str(r.get("DECAY")),
        "period_min": _num(r.get("PERIOD")),
        "inclination_deg": _num(r.get("INCLINATION")),
        "apogee_km": _num(r.get("APOGEE")),
        "perigee_km": _num(r.get("PERIGEE")),
        "rcs_size": _str(r.get("RCS_SIZE")),
        "comment": _str(r.get("COMMENT")),
        "debut": _str(r.get("DEBUT")),
    }
    rec["launch_id"] = launch_id(rec["intldes"])
    rec["constellation"] = constellation(rec["name"])
    classify_orbit(rec)
    return {k: v for k, v in rec.items() if v is not None}


def apply_gp(rec: dict, gp: dict) -> None:
    """Merge the latest GP (OMM/TLE) element set into a record, in place."""
    for key, src in [("period_min", "PERIOD"), ("inclination_deg", "INCLINATION"),
                     ("apogee_km", "APOAPSIS"), ("perigee_km", "PERIAPSIS"),
                     ("eccentricity", "ECCENTRICITY"), ("semimajor_axis_km", "SEMIMAJOR_AXIS"),
                     ("bstar", "BSTAR")]:
        val = _num(gp.get(src))
        if val is not None:
            rec[key] = round(val, 6) if key in ("eccentricity", "bstar") else round(val, 2)
    for key, src in [("epoch", "EPOCH"), ("tle1", "TLE_LINE1"), ("tle2", "TLE_LINE2")]:
        if gp.get(src):
            rec[key] = gp[src]
    if not rec.get("rcs_size") and gp.get("RCS_SIZE"):
        rec["rcs_size"] = gp["RCS_SIZE"]
    classify_orbit(rec)


def apply_refs(rec: dict, countries: dict[str, str], sites: dict[str, str]) -> None:
    cc, sc = rec.get("country_code"), rec.get("site_code")
    if cc:
        rec["country"] = countries.get(cc, cc)
    if sc:
        rec["site"] = sites.get(sc, sc)
