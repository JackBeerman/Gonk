"""Optional radio-frequency enrichment from the SatNOGS DB (https://db.satnogs.org).

Space-Track does not publish transmitter frequencies. SatNOGS is a community
database of satellite transmitters keyed by NORAD id. We download the full
transmitter list in a single request (at most once per day, cached in the state
directory) instead of querying per satellite. Newly launched satellites often
have no SatNOGS entry for days or weeks, so blanks are normal.

Set SATNOGS_API_TOKEN if your network or SatNOGS requires an API token;
set GONK_SATNOGS=0 to disable this lookup entirely.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import requests

from .store import age_days

log = logging.getLogger(__name__)
URL = "https://db.satnogs.org/api/transmitters/?format=json"


def _fmt_mhz(hz) -> str | None:
    return f"{hz / 1e6:.3f} MHz" if isinstance(hz, (int, float)) and hz else None


def summarize(tx: dict) -> str:
    """One short line: 'UHF telemetry 437.250 MHz (FSK 9600)'."""
    down = _fmt_mhz(tx.get("downlink_low"))
    up = _fmt_mhz(tx.get("uplink_low"))
    freq = f"down {down}" if down else ""
    if up:
        freq += (", " if freq else "") + f"up {up}"
    mode = tx.get("mode") or ""
    baud = tx.get("baud")
    extra = f" ({mode}{' ' + str(int(baud)) if baud else ''})" if mode else ""
    desc = tx.get("description") or tx.get("type") or "transmitter"
    return f"{desc}: {freq}{extra}".strip(": ")


def load_transmitters(cache_dir: Path, now: datetime) -> dict[int, list[dict]]:
    """Return {norad: [transmitter entry, ...]} for active transmitters, or {} if unavailable.

    Entries are the raw SatNOGS records; gonk.confidence.assess_rf rates each one using
    its `unconfirmed`, `frequency_violation`, `iaru_coordination` and `itu_notification` fields.
    """
    if os.environ.get("GONK_SATNOGS", "1") == "0":
        return {}
    cache = Path(cache_dir) / "satnogs_transmitters.json"
    meta = Path(cache_dir) / "satnogs_meta.json"
    fetched = json.loads(meta.read_text()).get("fetched") if meta.exists() else None
    if age_days(fetched, now) >= 1:
        headers = {"User-Agent": "gonk-orbit-report/1.0"}
        token = os.environ.get("SATNOGS_API_TOKEN")
        if token:
            headers["Authorization"] = f"Token {token}"
        try:
            resp = requests.get(URL, headers=headers, timeout=120)
            resp.raise_for_status()
            cache.write_text(resp.text)
            meta.write_text(json.dumps({"fetched": now.isoformat(timespec="seconds")}))
        except (requests.RequestException, ValueError) as e:
            log.warning("SatNOGS transmitter lookup unavailable: %s", e)
            if not cache.exists():
                return {}
    try:
        rows = json.loads(cache.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[int, list[dict]] = {}
    for tx in rows:
        norad = tx.get("norad_cat_id")
        if norad and tx.get("alive", True) and tx.get("status", "active") == "active":
            out.setdefault(int(norad), []).append(tx)
    return out
